# v0.2.0

- Added bounded headless agent with real planning and private scratch-file tools.
- Added native Chat and Responses agent transports, bounded turns, token output,
  tool calls, workspace size, total deadline and cancellation cleanup.
- Added authenticated in-memory upstream diagnostics with credential/URL redaction.
- 403 no longer falsely implies a proven Prompt Guard rejection.
- Mapped user-supplied leading system/developer messages into Responses instructions.
- Added stdlib smoke CLI and an explicit live-test report (generation still 403).
- Default API_MODE changed to eigent-agent. Set passthrough for v0.1 behavior.
- Agent runtime is independent Python code using attributed Eigent instructions,
  NOT full CAMEL/ListenChatAgent or Workforce. No cloud policy bypass.
