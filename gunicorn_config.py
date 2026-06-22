bind          = "127.0.0.1:8000"
workers       = 1          # must be 1 — daemon state and _calib dict are process-local
worker_class  = "gthread"  # threads let SSE hold a connection while other requests flow
threads       = 4          # 1 SSE stream + start/stop/cmd/data requests
timeout       = 0          # disable worker heartbeat timeout — SSE is long-lived
worker_tmp_dir = "/dev/shm"  # spare the SD card

errorlog  = "/var/log/metal_mapper/error.log"
accesslog = "/var/log/metal_mapper/access.log"
loglevel  = "info"
