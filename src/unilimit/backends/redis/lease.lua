-- Concurrency lease management for UniLimit.
-- KEYS[1]: lease storage key (contains {digest} for Redis Cluster slot pinning)
-- ARGV[1]: mode: "a" (acquire), "r" (renew), "x" (release)
-- ARGV[2]: lease_id (unique string)
-- ARGV[3]: now_us (integer microseconds, or 0 to query server TIME)
-- ARGV[4]: ttl_us (lease TTL in integer microseconds)
-- ARGV[5]: max_concurrent (integer)
-- ARGV[6]: key_ttl_sec (integer seconds for Redis key expiration)

local mode = ARGV[1]
local key = KEYS[1]
local lease_id = ARGV[2]

if mode == "x" then
    redis.call("ZREM", key, lease_id)
    return {1, 0, 0, 0}
end

local now_us = tonumber(ARGV[3])
if not now_us or now_us == 0 then
    local t = redis.call("TIME")
    now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])
end

local ttl_us = tonumber(ARGV[4])
local expires_at_us = now_us + ttl_us
local key_ttl = tonumber(ARGV[6])

-- Purge expired leases
redis.call("ZREMRANGEBYSCORE", key, "-inf", now_us)

if mode == "r" then
    local score = redis.call("ZSCORE", key, lease_id)
    if score then
        redis.call("ZADD", key, expires_at_us, lease_id)
        redis.call("EXPIRE", key, key_ttl)
        return {1, 0, 0, 0}
    end
    return {0, 0, 0, 0}
end

-- mode == "a" (acquire)
local max_c = tonumber(ARGV[5])
local existing = redis.call("ZSCORE", key, lease_id)
if existing then
    redis.call("ZADD", key, expires_at_us, lease_id)
    redis.call("EXPIRE", key, key_ttl)
    local count = redis.call("ZCARD", key)
    return {1, count, max_c, 0}
end

local count = redis.call("ZCARD", key)
if count >= max_c then
    local earliest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    local retry_after = 0
    if earliest and #earliest >= 2 then
        local exp = tonumber(earliest[2])
        if exp > now_us then
            retry_after = exp - now_us
        end
    end
    return {0, count, max_c, retry_after}
end

redis.call("ZADD", key, expires_at_us, lease_id)
redis.call("EXPIRE", key, key_ttl)
return {1, count + 1, max_c, 0}
