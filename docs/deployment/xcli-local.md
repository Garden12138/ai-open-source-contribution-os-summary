# Native xcli Model Gateway connection

This optional transport runs only when the API and model gateway share a native host network namespace. Set `CONTRIBOS_XCLI_LOCAL_ENDPOINT` explicitly in both processes to the installed gateway's loopback IP, actual port and `/v1` path. There is no default registration. For example, a local test installation can use `http://127.0.0.1:18741/v1`; use the port reported by its installation.

The model settings page then offers `xcli 本机网关`. Its endpoint is fixed to the operator registration. Save a separately issued Contribution OS application Token through the existing sealed credential workflow, then create a model Profile with an explicit xcli alias, output budget and structured-output mode. Existing providers retain public HTTPS validation; changing their type cannot make an arbitrary private URL acceptable.

The model gateway verifies the existing short-lived task Token and frozen Profile. Before sending task text, it reads the authenticated xcli catalog and checks the frozen alias plus verified Tools/JSON Schema capabilities. Local calls have bounded request/response sizes, no environment proxy, no redirects, and no automatic transport retry. Error messages exclude remote bodies and credentials. The original encrypted credential store, task-scope authorization and audit chain continue to apply.

An xcli gateway may forward to remote or unknown upstreams. Choose the application Token's allowed models and remote permission explicitly; the loopback address does not establish local inference. This client does not supply a per-request remote-consent header, so Tokens requiring that header reject remote inference.

Containers are explicitly rejected. Do not set this variable in the Docker Compose deployment, bind xcli to a LAN address, or expose it to Sandbox Worker networks. A fixed-purpose, authenticated Host Bridge requires a separate design and acceptance run. This transport does not implement that bridge.

Offline checks use synthetic inputs: `python -m pytest tests/test_xcli_local.py tests/test_model_settings.py tests/test_provider_gateway.py tests/test_task_model_selection.py`. Real Plan/Review, provider quality, container Bridge and cross-platform deployment gates remain open. Do not switch an active deployment or issue a real credential as part of offline checks.
