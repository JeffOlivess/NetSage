NetSage datasets
================

These are the exact datasets the two deep neural networks were trained and tested on.
They are synthetic: generated from documented fault signatures with randomised baselines
(3 ms campus LAN up to 120 ms mobile hotspot), overlapping class ranges and measurement
noise. Regenerate them with:  python scripts/export_dataset.py


1. diagnosis_dataset.csv   (Fault Diagnoser)
   24,000 rows = 3,000 samples x 8 classes. Split 80/20: 19,200 train, 4,800 test.

   gw_latency     gateway round-trip time (ms, 1000 = no reply)
   gw_loss        gateway packet loss (%)
   inet_latency   internet round-trip time (ms, 1000 = no reply)
   inet_loss      internet packet loss (%)
   jitter         variation of internet latency (ms)
   lat_ratio      internet latency / healthy baseline of the link (x)
   dns_fail       DNS queries failing (%)
   signal         Wi-Fi signal (%)
   link_up        1 = link up, 0 = link down
   util           utilisation of the busiest link on the path (%)
   err_rate       interface error rate (%)
   cpu            CPU of the busiest device on the path (%)
   fault_class    label: HEALTHY, LINK_DOWN, PACKET_LOSS, CONGESTION, UPSTREAM_OUTAGE,
                  DNS_FAILURE, WEAK_SIGNAL, DEVICE_OVERLOAD
   split          train or test
   model_prediction, model_confidence   output of the trained diagnoser for that row
   (filter split = test and compare fault_class with model_prediction: 99.56% match)


2. timeseries_dataset.csv   (Fault Predictor)
   300 telemetry time series of 90 samples each (27,000 rows): healthy periods,
   gradual fault build-ups, abrupt failures and short harmless spikes.

   sequence_id, t           which series and which sample (1 sample = 1 second)
   (12 feature columns)     same as above
   fault_active             1 if a fault is in progress at this sample
   fault_within_15          the predictor's target: 1 if a fault is active now or
                            within the next 15 samples
   The predictor reads 10 consecutive samples (levels + trends) and estimates
   fault_within_15.
