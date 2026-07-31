# Histogram Metrics

This is about the Couchbase metrics themselves rather than about Promtimer. For how to declare a
heatmap panel, see the `heatmap` base in [the dashboarding README](README.md).

A Prometheus histogram metric `M` exposes three series: `M_bucket`, a cumulative count labelled by
the bucket's upper bound `le`, along with `M_sum` and `M_count`. Several different panels can be
built from that triple, for a metric `M` and a label selector `S`:

| View | Expression |
| --- | --- |
| Throughput | `irate(M_count{S}[5m])` |
| Average | `irate(M_sum{S}[5m]) / ignoring(name) irate(M_count{S}[5m])` |
| Distribution | `sum by (le)(increase(M_bucket{S}[5m]))` |
| Count over a threshold | `increase(M_bucket{S,le="+Inf"}[5m]) - ignoring(le) increase(M_bucket{S,le="<bound>"}[5m])` |
| Quantile | `histogram_quantile(0.99, sum by (le)(rate(M_bucket{S}[5m])))` |

## Bucket Resolution Differs by Component

How much a given view can be trusted depends on how finely the component buckets its observations,
and Couchbase components differ substantially here.

ns_server histograms, which carry the `cm_` prefix, are bucketed by `ceil(log10(value))` -- see
`get_histogram_bucket` in `ns_server_stats.erl`. Bucket bounds are therefore consecutive powers of
ten. Most of these metrics use the default ceiling of 10000 milliseconds, which gives six series in
total: `le="0.001"`, `"0.01"`, `"0.1"`, `"1.0"`, `"10.0"` and `"+Inf"`. Each bucket spans a full
order of magnitude.

KV histograms, which carry the `kv_` prefix, are backed by HdrHistogram and have far more, much
narrower buckets.

This matters most for `histogram_quantile`, which interpolates linearly *within* the bucket that a
quantile falls into. On KV metrics that interpolation covers a narrow range and the result is
meaningful. On `cm_` metrics the bucket containing the quantile may span 100ms to 1s, so the
returned value reflects where Prometheus assumes the samples lie within that bucket rather than
where they actually lie.

For `cm_` metrics, prefer the distribution and threshold-count views. Both read bucket counts
directly and never interpolate. A distribution shows the shape at the resolution the data actually
has, and a threshold count placed on a bucket bound is exact.

## Matching an le Bound Exactly

`le` is a string label, so the threshold-count view only works when the literal matches an exposed
bound exactly. A selector that matches no bound matches no series, and the panel is silently empty
rather than failing.

ns_server formats bounds with `to_seconds_bin` in `ns_server_stats.erl`. Metrics recorded in
milliseconds or microseconds render as floats, so a one second bound is `le="1.0"` and ten seconds
is `le="10.0"`. Only metrics recorded in whole seconds render as integers, as `le="1"`.

A metric whose ceiling falls below the chosen threshold has no matching bound at all, and
contributes nothing to the panel.

## Summing Over le

The distribution view needs `sum by (le)`. Without it, every other label on the metric -- node,
bucket, op and so on -- produces an additional overlapping series and the heatmap becomes
unreadable. A heatmap has to resolve to a single series per `le` value, so metrics carrying other
labels need those either aggregated away or pinned by the selector or a template variable.

## ns_server Histogram Metrics

Every ns_server histogram is exposed as `cm_<name>_seconds`. The ceiling shown is the one passed at
the call site; those using `notify_histogram/2` take the default of 10000 milliseconds.

| Metric | Labels | Range | Recorded in |
| --- | --- | --- | --- |
| `cm_memcached_call_time_seconds` | `bucket` | 1ms - 10s | `ns_memcached.erl` |
| `cm_memcached_q_call_time_seconds` | `bucket` | 1ms - 10s | `ns_memcached.erl` |
| `cm_memcached_e2e_call_time_seconds` | `bucket` | 1ms - 10s | `ns_memcached.erl` |
| `cm_chronicle_disk_latency_seconds` | `op` | set by chronicle | `chronicle_local.erl` |
| `cm_http_requests_seconds` | | 1ms - 10s | `menelaus_util.erl` |
| `cm_outgoing_http_requests_seconds` | `type` | 1ms - 10s | `rest_utils.erl`, `prometheus.erl` |
| `cm_status_latency_seconds` | | 1ms - 10s | `ns_heart.erl` |
| `cm_timer_lag_seconds` | | 1ms - 10s | `timer_lag_recorder.erl` |
| `cm_ns_config_merger_run_time_seconds` | | 1ms - 10s | `ns_config_rep.erl` |
| `cm_ns_config_merger_sleep_time_seconds` | | 1ms - 10s | `ns_config_rep.erl` |
| `cm_gc_duration_seconds` | | 1us - 1s | `ns_gc_runner.erl` |
| `cm_mru_cache_<call>_time_seconds` | `name`, `type` | 1us - 1s | `mru_cache.erl` |
| `cm_lease_acquirer_<name>_seconds` | `node` | 1ms - 10s | `leader_lease_acquire_worker.erl` |

The three memcached metrics are a decomposition rather than three views of one measurement.
`cm_memcached_q_call_time_seconds` covers the wait before `ns_memcached` issues a call,
`cm_memcached_call_time_seconds` covers the call itself, and `cm_memcached_e2e_call_time_seconds`
covers both. Comparing them separates memcached being slow from ns_server's own workers backing up.
