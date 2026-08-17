# Multi-Device Access

BrainMem can expose its MCP tools over an optional HTTP/SSE transport for users
who want several devices to use the same memory base. This transport is opt-in.
The existing stdio MCP server and existing client configurations are unchanged.

The HTTP server fixes `brain_root` on the server at startup. Remote clients do
not choose or override the data root. HTTP changes only the transport path; it
does not change BrainMem's provider consent, review, or privacy rules.

## When To Use HTTP Transport

Use HTTP/SSE when one always-on machine owns the BrainMem data root and other
trusted devices need MCP access to it:

- a laptop, desktop, and travel device sharing one personal memory base;
- a home server, mini PC, NAS, or plugged-in laptop acting as the BrainMem host;
- a small trusted team or household setup where one server is easier to manage
  than copying runtime memory data between machines.

Keep using stdio when the MCP client runs on the same machine as the data root,
or when you do not need cross-device access.

## Architecture And Data Flow

```text
[Remote MCP client]
  Claude Desktop / Cursor / Codex / Cline
        |
        |  MCP over HTTP/SSE
        |  X-Brainmem-Token header (required off loopback by default)
        v
[BrainMem HTTP server]
  mem-mcp-http
  fixed server-side BRAIN_ROOT
        |
        |  local filesystem + SQLite
        v
[BrainMem data root]
  events.jsonl, brain.db, raw/, laundry/, pages/, review/
```

The server process is the only component that reads and writes the BrainMem
runtime data. Clients call tools through MCP. They should not mount, sync, or
directly edit the server's data root.

TLS is not built into BrainMem. Transport encryption and network exposure are
the responsibility of the outer network layer, such as Tailscale, Cloudflare
Tunnel, or a reverse proxy with TLS termination. A shared token authenticates
requests but does not encrypt traffic by itself.

## Recommended Topology: Tailscale

The recommended topology is one always-on BrainMem host on a private Tailscale
network. Other devices connect to the host by its stable Tailscale MagicDNS
name.

```text
[Always-on host: mini PC / NAS / plugged-in laptop]
  BRAIN_ROOT=<server-brain-root>
  BRAINMEM_TOKEN=<random-long-token>
  mem-mcp-http --host 0.0.0.0 --port 8765
              |
              |  Tailscale private WireGuard network
              |
   +----------+-------------+---------------+
   |                        |               |
[Work laptop]          [Desktop]       [Travel device]
 Codex CLI              Cursor          Claude Desktop
```

Why this is the default recommendation:

- BrainMem data stays on a machine you control.
- The HTTP endpoint does not need to be exposed to the public internet.
- Tailscale handles device identity, encrypted transport, MagicDNS, and ACLs.
- Client config can use a stable hostname instead of a changing address.

Use Tailscale's own documentation for installation, device approval, MagicDNS,
and ACL setup. BrainMem only needs a reachable HTTP/SSE URL inside that network.

## Server Startup

`mem-mcp-http` starts the HTTP/SSE MCP server. The command is separate from the
stdio `mem-mcp` entry point.

```sh
BRAIN_ROOT=<server-brain-root> \
BRAINMEM_TOKEN=<random-long-token> \
mem-mcp-http --host 0.0.0.0 --port 8765
```

The server must be started with a data root by either `--brain-root` or
`BRAIN_ROOT`. In HTTP mode this value is fixed for the process and is not
accepted from clients.

| Option | Environment variable | Purpose |
| --- | --- | --- |
| `--brain-root <path>` | `BRAIN_ROOT` | Server-owned BrainMem data root. Required for HTTP mode. |
| `--host <host>` | `BRAINMEM_HOST` | Bind address. Use `0.0.0.0` for private-network access or loopback for local testing. |
| `--port <port>` | `BRAINMEM_PORT` | HTTP/SSE listening port. |
| `--token-env <name>` | n/a | Name of the environment variable that stores the shared token. |
| `--allow-unauthenticated` | n/a | Explicitly allow tokenless non-loopback access. Unsafe unless an outer trusted network supplies access control. |
| `--enable-tool <name>` | n/a | Opt in a remote tool that is disabled by default. |
| `--disable-tool <name>` | n/a | Remove a tool from remote exposure. |
| `--log-level <level>` | n/a | Adjust server logging. |

The default bind address is `127.0.0.1`, where tokenless local testing is
allowed with a warning. For any non-loopback bind, startup fails when the token
environment variable is missing or empty. `--allow-unauthenticated` is an
explicit escape hatch for a controlled network that already provides access
control; do not use it for direct public exposure.

## Client Configuration

Configure the remote server as an SSE MCP server. Exact file locations vary by
client, so use each client's current documentation for where to place MCP
configuration.

```json
{
  "mcpServers": {
    "brainmem-remote": {
      "transport": "sse",
      "url": "http://<tailscale-hostname>:8765/sse",
      "headers": {
        "X-Brainmem-Token": "<same-token-as-server>"
      }
    }
  }
}
```

Notes:

- Prefer a Tailscale MagicDNS hostname for `<tailscale-hostname>`.
- Do not put private hostnames, addresses, usernames, paths, or tokens in shared
  repository files.
- A client may keep both a local stdio server named `brainmem` and a remote SSE
  server named `brainmem-remote`. Agent instructions should say which one to
  use for a given task.
- Remote clients cannot pass `brain_root` to select server paths. The server's
  startup configuration decides the data root.

## Authentication Model

The first HTTP transport uses a minimal shared-token model:

- The server reads the token from an environment variable such as
  `BRAINMEM_TOKEN`.
- Clients send the same value in the `X-Brainmem-Token` header.
- Token comparison is exact and should be implemented with constant-time
  comparison.
- Tokens must not be printed, committed, stored in docs, or returned in errors.
- Rotate a token by changing it on the server and updating each client config.
- A non-loopback bind without a token is rejected at startup unless
  `--allow-unauthenticated` is explicitly passed.

Use a long random token. For example, generate one with your operating system's
password manager, a secrets manager, or a local command that produces
cryptographically random bytes.

Authentication does not replace network protection. Use Tailscale ACLs, a
private tunnel, or a reverse proxy policy to limit which devices can reach the
endpoint.

## Remote Tool Exposure

Remote mode exposes only a whitelist of tools. The intent is to keep everyday
read and low-risk write workflows usable while avoiding high-impact review
actions over a network transport.

Default remote tools are expected to include:

- status and recent-event inspection;
- local or provider-backed recall, subject to the same consent rules as CLI use;
- injection context generation;
- capture and scratch append;
- deterministic snapshot rebuild;
- procedure listing and procedure run.

High-risk review apply tools are not remotely exposed. They can rewrite or
batch-apply memory changes and must stay local and explicitly user-controlled.

`procedure_new` and `procedure_promote` are opt-in for remote mode. They create
or change reusable procedure state, so deployments should enable them only when
the trusted client workflow really needs them.

## Privacy And Consent Boundary

HTTP/SSE changes only how the MCP request reaches BrainMem. It does not change
what each command is allowed to do.

Local-only operations remain local-only when the server executes them, including
keyword-only recall, status checks, scratch append, snapshot rebuild,
deterministic lint/rebuild, and cost estimates.

Provider-backed operations still require clear permission for sensitive or
user-provided content, including default hybrid recall that embeds the query,
semantic recall, `ask --explain`, ingest, reindex, promote-chat, and review
apply flows that rewrite compiled truth.

Review queue decisions remain human-controlled. Agents may inspect or summarize
review items when asked, but must not approve, reject, or apply them unless the
user explicitly asks for that exact action.

## Alternative Topologies

### Cloudflare Tunnel

Cloudflare Tunnel can help when a client cannot join a private mesh network.
The tradeoff is that traffic transits a third-party service and configuration
must be reviewed carefully. Use access policies and keep the BrainMem shared
token enabled.

### Reverse Proxy With TLS

A reverse proxy with TLS termination can work for users who already operate a
domain and certificate automation. Put BrainMem behind the proxy, restrict
access, forward only the MCP endpoint, and keep tokens out of proxy logs. This
is more operationally demanding than Tailscale.

### Local Loopback

Loopback HTTP can be useful for debugging an SSE client on the same host. It is
the only mode that allows missing token configuration by default. Both IPv4
loopback (`127.0.0.0/8`) and IPv6 loopback (`::1`) are recognized. It is not a
multi-device topology, and stdio remains the simpler default for normal local
use.

## Not Recommended

Avoid these patterns:

- exposing `mem-mcp-http` directly to the public internet with router port
  forwarding;
- using `--allow-unauthenticated` on a network that lacks independent access
  control;
- relying on a public development tunnel as a permanent production path;
- placing the BrainMem data root on a public VPS just to make it reachable;
- syncing `brain.db`, `events.jsonl`, or the runtime data root with generic file
  sync while the server is running;
- running several write-capable BrainMem processes against the same data root
  without a clear operating procedure.

## Troubleshooting

If the client cannot connect:

- Confirm the server is listening on the expected host and port.
- Confirm the client URL ends in `/sse`.
- Check local firewall rules on the server.
- Check Tailscale device status, MagicDNS, ACLs, and whether both devices are
  in the same tailnet or shared network.
- Verify the token header name is `X-Brainmem-Token` and the value matches the
  server token.
- Restart or reload the MCP client after changing configuration.
- Confirm the server process can read and write its configured data root.
- Check server logs for a missing token warning, startup validation failure, or
  rejected request.

If tools are missing:

- Confirm the tool is in the remote whitelist.
- Remember that review apply tools are intentionally unavailable remotely.
- Enable opt-in tools only when the deployment's agent policy allows them.

If results look like the wrong memory base:

- Check the server's startup `BRAIN_ROOT`.
- Do not try to pass `brain_root` from the client; remote mode ignores client
  path selection by design.

## Limits And Known Issues

- BrainMem does not provide built-in TLS. Use a private mesh, tunnel, or reverse
  proxy for encrypted transport.
- Shared-token authentication is intentionally simple. It is not OAuth, a user
  system, or fine-grained authorization.
- Remote mode fixes one server data root per server process.
- Concurrent reads are expected to be safe, but multiple write-capable
  processes against the same data root can still create operational risk.
- Startup may warn about possible concurrent writers, but the first HTTP
  transport does not add a distributed lock or write queue.
- Cloudflare Tunnel and similar alternatives may route traffic through a third
  party.
- Existing stdio behavior is unchanged; users who do not opt in to HTTP/SSE do
  not need to change any client configuration.
