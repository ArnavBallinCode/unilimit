from __future__ import annotations

from django.urls import path

from tests.django_integration import views as v

urlpatterns = [
    path("ok", v.ok),
    path("aok", v.aok),
    path("sync", v.limited_sync),
    path("async", v.limited_async),
    path("user", v.by_user),
    path("user-async", v.by_user_async),
    path("user-or-ip", v.user_or_ip),
    path("multi", v.multi),
    path("pair", v.pair),
    path("header", v.by_header),
    path("tenant", v.by_tenant),
    path("post", v.post_only),
    path("shares-global", v.shares_global),
    path("unnamed", v.unnamed),
    path("own-headers", v.own_headers),
    path("stream", v.stream),
    path("astream", v.astream),
    path("acbv", v.AsyncCBV.as_view()),
    path("scbv", v.SyncCBV.as_view()),
    path("method-cbv", v.MethodCBV.as_view()),
    path("api/anon-user", v.AnonAndUser.as_view()),
    path("api/scoped", v.Scoped.as_view()),
    path("api/scoped-other", v.ScopedOther.as_view()),
    path("api/unscoped", v.Unscoped.as_view()),
    path("api/rules", v.WithRules.as_view()),
    path("api/two", v.TwoThrottles.as_view()),
    path("api/custom", v.CustomIdentity.as_view()),
    path("api/strict", v.Strict.as_view()),
    path("api/mixed", v.Mixed.as_view()),
    path("api/mixed-scoped", v.MixedScoped.as_view()),
    path("api/shared", v.ThrottleAndDecorator.as_view()),
    path("api/decorated", v.DecoratedAPIView.as_view()),
]
