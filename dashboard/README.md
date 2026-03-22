# Dashboard

Static operator dashboard served by [`dashboard_server.py`](/F:/InformationAndCourses/Code/node-controller/dashboard_server.py).

Recommended runtime:

```bash
python dashboard_server.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --host 127.0.0.1 \
  --port 8787
```

For long-running use on `dev-intern-01/02`, prefer the tmux wrapper scripts under [`scripts/`](/F:/InformationAndCourses/Code/node-controller/scripts).
