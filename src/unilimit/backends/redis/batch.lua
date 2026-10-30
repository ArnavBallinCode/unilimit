-- UniLimit batch script, Redis key schema v1.
--
-- Port of the Python reference implementations in unilimit/algorithms and of
-- unilimit.backends.base.evaluate_batch / peek_batch. Any change must keep the
-- results and stored state identical to the Python model; the differential
-- tests in tests/redis_backend enforce this.
--
-- Numbers are IEEE-754 doubles, exactly like Python floats. Every floating-point
-- expression below uses the same operands, in the same order, as the Python code.
-- All stored or returned numbers go through '%.17g' (Lua's tostring is lossy).
--
-- KEYS[i]  : storage key of op i (all KEYS of one call share a hash slot in Cluster)
-- ARGV[1]  : 'c' = consume (evaluate, commit iff every enforcing op allows), 'p' = peek
-- ARGV[2]  : now in microseconds, or '' (empty) to use the server clock (TIME)
-- ARGV[3 + 6(i-1) .. 8 + 6(i-1)] : algorithm code, limit, window_us, capacity, cost, enforce
--
-- Reply: {committed, now, then per op: allowed, remaining, reset_after_us,
--         retry_after_us (-1 = never), ttl_us (0 unless written),
--         written state string ('' unless written)}
-- On unusable stored state nothing is written and the reply is
--         {-1, 'MISMATCH' | 'CORRUPT', key, stored_algorithm_code}.

local EPS = 1e-9
local MAX_EXACT = 9007199254740991

local function fmt(x)
  return string.format('%.17g', x)
end

local function nonneg_int(s)
  local n = tonumber(s)
  if n == nil or n > MAX_EXACT then
    return nil
  end
  return n
end

-- Fixed window ------------------------------------------------------------------------

local function fw_decode(raw)
  local s, c = string.match(raw, '^fw:(%d+):(%d+)$')
  s, c = nonneg_int(s), nonneg_int(c)
  if s == nil or c == nil then
    return nil
  end
  return { s, c }
end

local function fw_active(st, now, window)
  if st ~= nil and now < st[1] + window then
    return st
  end
  return nil
end

local function fw_evaluate(st, now, limit, window, capacity, cost)
  local start, count = now, 0
  local active = fw_active(st, now, window)
  if active ~= nil then
    start, count = active[1], active[2]
  end
  local left = window - math.max(0, now - start)
  if count + cost > limit then
    local reset = 0
    if count > 0 then
      reset = left
    end
    local retry = left
    if cost > limit then
      retry = -1
    end
    return { 0, limit - count, reset, retry }
  end
  local new_count = count + cost
  return { 1, limit - new_count, left, 0, 'fw:' .. fmt(start) .. ':' .. fmt(new_count), left }
end

local function fw_peek(st, now, limit, window, capacity, cost)
  local active = fw_active(st, now, window)
  if active == nil then
    if cost <= limit then
      return { 1, limit, 0, 0 }
    end
    return { 0, limit, 0, -1 }
  end
  local count = active[2]
  local left = window - math.max(0, now - active[1])
  if count + cost <= limit then
    return { 1, limit - count, left, 0 }
  end
  local retry = left
  if cost > limit then
    retry = -1
  end
  return { 0, limit - count, left, retry }
end

-- Sliding window counter --------------------------------------------------------------

local function sw_decode(raw)
  local i, c, p = string.match(raw, '^sw:(%d+):(%d+):(%d+)$')
  i, c, p = nonneg_int(i), nonneg_int(c), nonneg_int(p)
  if i == nil or c == nil or p == nil then
    return nil
  end
  return { i, c, p }
end

-- Returns index, current, previous, elapsed after window rotation.
local function sw_normalise(st, now, window)
  -- Exact for now < 2^53: floor(a / b) of correctly rounded division equals a // b.
  local index = math.floor(now / window)
  if st == nil then
    return index, 0, 0, now - index * window
  end
  local stored, current, previous = st[1], st[2], st[3]
  if stored == index then
    return index, current, previous, now - index * window
  end
  if stored == index - 1 then
    return index, 0, current, now - index * window
  end
  if stored < index - 1 then
    return index, 0, 0, now - index * window
  end
  return stored, current, previous, 0
end

local function sw_estimate(current, previous, elapsed, window)
  local weight = (window - elapsed) / window
  return previous * weight + current
end

local function sw_reset_after(current, previous, elapsed, window)
  if current > 0 then
    return 2 * window - elapsed
  end
  if previous > 0 then
    return window - elapsed
  end
  return 0
end

local function sw_retry_after(current, previous, elapsed, window, limit, cost)
  if cost > limit then
    return -1
  end
  if current + cost <= limit then
    if previous == 0 then
      return 0
    end
    local t = (window - elapsed) - (limit - current - cost) / previous * window
    return math.max(0, math.ceil(t))
  end
  local t_next = window - (limit - cost) / current * window
  return (window - elapsed) + math.max(0, math.ceil(t_next))
end

local function sw_evaluate(st, now, limit, window, capacity, cost)
  local index, current, previous, elapsed = sw_normalise(st, now, window)
  local est = sw_estimate(current, previous, elapsed, window)
  if est + cost > limit + EPS then
    return {
      0,
      math.max(0, math.floor(limit - est + EPS)),
      sw_reset_after(current, previous, elapsed, window),
      sw_retry_after(current, previous, elapsed, window, limit, cost),
    }
  end
  local ttl = 2 * window - elapsed
  return {
    1,
    math.max(0, math.floor(limit - est - cost + EPS)),
    ttl,
    0,
    'sw:' .. fmt(index) .. ':' .. fmt(current + cost) .. ':' .. fmt(previous),
    ttl,
  }
end

local function sw_peek(st, now, limit, window, capacity, cost)
  local _, current, previous, elapsed = sw_normalise(st, now, window)
  local est = sw_estimate(current, previous, elapsed, window)
  local remaining = math.max(0, math.floor(limit - est + EPS))
  local reset = sw_reset_after(current, previous, elapsed, window)
  if est + cost <= limit + EPS then
    return { 1, remaining, reset, 0 }
  end
  return { 0, remaining, reset, sw_retry_after(current, previous, elapsed, window, limit, cost) }
end

-- Token bucket ------------------------------------------------------------------------

local function tb_decode(raw)
  local t, l = string.match(raw, '^tb:([^:]+):(%d+)$')
  t, l = tonumber(t), nonneg_int(l)
  if t == nil or l == nil or t ~= t or t < 0 or t == math.huge then
    return nil
  end
  return { t, l }
end

local function tb_refill(st, now, limit, window, capacity)
  local rate = limit / window
  if st == nil then
    return capacity, now, rate
  end
  local tokens, last = st[1], st[2]
  if now <= last then
    return math.min(capacity, tokens), last, rate
  end
  tokens = tokens + (now - last) * rate
  if tokens >= capacity - EPS then
    tokens = capacity
  end
  return tokens, now, rate
end

local function tb_until(needed, rate)
  if needed > 0 then
    return math.max(0, math.ceil(needed / rate))
  end
  return 0
end

local function tb_evaluate(st, now, limit, window, capacity, cost)
  local tokens, last, rate = tb_refill(st, now, limit, window, capacity)
  if tokens + EPS >= cost then
    local new_tokens = math.max(0, tokens - cost)
    local reset = tb_until(capacity - new_tokens, rate)
    return {
      1,
      math.floor(new_tokens + EPS),
      reset,
      0,
      'tb:' .. fmt(new_tokens) .. ':' .. fmt(last),
      math.max(1, reset),
    }
  end
  local retry = -1
  if cost <= capacity then
    retry = tb_until(cost - tokens, rate)
  end
  return { 0, math.floor(tokens + EPS), tb_until(capacity - tokens, rate), retry }
end

local function tb_peek(st, now, limit, window, capacity, cost)
  local tokens, _, rate = tb_refill(st, now, limit, window, capacity)
  local retry
  local allowed = 0
  if tokens + EPS >= cost then
    allowed, retry = 1, 0
  elseif cost > capacity then
    retry = -1
  else
    retry = tb_until(cost - tokens, rate)
  end
  return { allowed, math.floor(tokens + EPS), tb_until(capacity - tokens, rate), retry }
end

-- Batch -------------------------------------------------------------------------------

local ALGORITHMS = {
  fw = { decode = fw_decode, evaluate = fw_evaluate, peek = fw_peek },
  sw = { decode = sw_decode, evaluate = sw_evaluate, peek = sw_peek },
  tb = { decode = tb_decode, evaluate = tb_evaluate, peek = tb_peek },
}

local mode = ARGV[1]
local now = tonumber(ARGV[2])
if ARGV[2] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000000 + tonumber(t[2])
end

local ops = {}
for i = 1, #KEYS do
  local b = 3 + (i - 1) * 6
  local op = {
    impl = ALGORITHMS[ARGV[b]],
    limit = tonumber(ARGV[b + 1]),
    window = tonumber(ARGV[b + 2]),
    capacity = tonumber(ARGV[b + 3]),
    cost = tonumber(ARGV[b + 4]),
    enforce = ARGV[b + 5] == '1',
    state = nil,
  }
  local raw = redis.call('GET', KEYS[i])
  if raw then
    local stored_code = string.sub(raw, 1, 2)
    if string.sub(raw, 3, 3) == ':' and ALGORITHMS[stored_code] ~= nil and stored_code ~= ARGV[b] then
      return { -1, 'MISMATCH', KEYS[i], stored_code }
    end
    op.state = op.impl.decode(raw)
    if op.state == nil then
      return { -1, 'CORRUPT', KEYS[i], '' }
    end
  end
  ops[i] = op
end

local function call(fn, op)
  return fn(op.state, now, op.limit, op.window, op.capacity, op.cost)
end

local committed = 0
local results = {}
if mode == 'c' then
  committed = 1
  for i = 1, #ops do
    results[i] = call(ops[i].impl.evaluate, ops[i])
    if ops[i].enforce and results[i][1] == 0 then
      committed = 0
    end
  end
end

if committed == 1 then
  for i = 1, #ops do
    local r = results[i]
    if r[1] == 1 then
      -- PX is in milliseconds and Redis measures it from the script's start, which can
      -- be up to 1 ms before `now` at millisecond resolution. Rounding up plus 1 ms
      -- guarantees a key never expires before the model's TTL. Expiring later is always
      -- safe: the model's state no longer matters once its TTL has passed.
      redis.call('SET', KEYS[i], r[5], 'PX', fmt(math.ceil(r[6] / 1000) + 1))
    end
  end
else
  for i = 1, #ops do
    results[i] = call(ops[i].impl.peek, ops[i])
  end
end

local reply = { committed, now }
for i = 1, #ops do
  local r = results[i]
  local ttl, written = 0, ''
  if committed == 1 and r[1] == 1 then
    ttl, written = r[6], r[5]
  end
  reply[#reply + 1] = r[1]
  reply[#reply + 1] = r[2]
  reply[#reply + 1] = r[3]
  reply[#reply + 1] = r[4]
  reply[#reply + 1] = ttl
  reply[#reply + 1] = written
end
return reply
