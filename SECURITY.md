# Security

## What this is

hide-my-list = personal AI agent with Notion API creds, runs unattended on VM. Not a bank — but unattended AI warrants security thought.

Infrastructure sections name specific tech (Tailscale, Squid, UFW, Gravwell) — that's what we run. Principles matter more: deny-by-default egress, domain allowlisting, credential isolation, network segmentation. Implement with whatever fits your stack.

## Agent trust model

AI agents have fundamental problem: prompt injection unsolved. Any agent processing untrusted input can be manipulated. Meta's [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) gives useful framework. Agent becomes dangerous when combining all three:

- **[A]** Processes untrusted input
- **[B]** Accesses sensitive data or credentials
- **[C]** Changes state or communicates externally

Goal: no single session or component combines all three without human in loop. How hide-my-list components break down:

### Python app — [BC] with a constrained surface

Python app container has access to credentials [B] and makes outbound state changes [C]. Conversational interface authenticated via Signal pairing — only owner can send messages. Direct user interaction = **[BC]**: sensitive data + state changes, trusted input source.

Tools are constrained by design. The app has no `fetch_url`, no shell tool, no `git pull`, no self-modification surface. Tool surface is limited to Notion CRUD, signal-cli delivery, and LLM calls. Even under prompt injection, an attacker cannot break out to the host or reach arbitrary URLs — the constrained tool surface is the containment boundary.

Caveat: **GitHub content**. CI/CD review agents process PR descriptions, issue bodies, and code content — potential injection vectors. If an attacker contributes to the repo, that's untrusted input reaching the review pipeline. Real risk. Proxy prevents the main app from browsing arbitrary web content, but CI agents are a separate surface.

#### Why the GitHub tradeoff?

The multi-agent review pipeline (`.github/workflows/`) evaluates and applies changes to prompt and spec files, with a psych reviewer validating user-facing changes against ADHD research. External review adds quality assurance that no single-agent authoring loop can provide. GitHub access is scoped to what the pipeline needs.

### Reminder delivery — [BC] configuration

`app/scheduler/reminder_worker.py` + Postgres `reminder_outbox` state machine. Worker claims due rows with `SELECT FOR UPDATE SKIP LOCKED`, delivers via signal-cli. Loads only Notion credentials **[B]** for reminder completion calls and Signal credentials **[C]** for delivery. Processes no untrusted input **[A]** — reads only structured Notion data and Postgres rows created by the app itself. Safe **[BC]** configuration.

### CI/CD review agents — [AC] configuration

PR review agents process untrusted code **[A]** and write PR comments **[C]**, no access to infrastructure secrets or Notion credentials **[B]**. Workflows use only repo-scoped GitHub token permissions (contents/pull-requests/issues write for posting reviews). Safe [AC] — even if malicious PR manipulates review agent, no sensitive data to exfiltrate.

Additional CI/CD controls:
- Fork PRs blocked from triggering workflows on self-hosted runners (all workflows check `github.event.pull_request.head.repo.full_name == github.repository`; review pipeline dispatch to self-hosted runners additionally requires author to be collaborator/member/owner)
- Devcontainer image built only from main branch, never from PR branches
- Review agents run on self-hosted homelab runners with no infrastructure credentials; runners isolated by same VLAN segmentation and proxy controls as main agent host
- Security gate jobs (`authorize`, `get-context`, `check-failure`, `get-pr-context`) stay on `ubuntu-latest` to process untrusted input before dispatching to self-hosted runners

### Prompt injection risk

Main risk isn't host access — admin surfaces locked behind Tailscale, file permissions fine. Real risk: agent is an LLM that could be prompted into acting for adversary. May include credentials, but really about data in Notion or other systems agent accesses; credentials are just how data gets reached.

We use frontier-lab-hosted models expecting their safety alignment resists prompt injection. Research not reassuring. Studies show more capable models can be *more* susceptible — same instruction-following ability that makes them useful makes them better at following injected instructions ([Li et al., EMNLP 2024](https://aclanthology.org/2024.emnlp-main.33/)). Some frontier models show better resistance via alignment — Claude 3 resisted direct injection in [multimodal prompt injection testing](https://arxiv.org/html/2509.05883v1) where GPT-4o, Gemma, LLaMA did not — but no model reliably defends via alignment alone. Model-level resistance = speed bump, not wall.

## Mitigations

Mitigations below constrain what manipulated agent could do.

### Limited credentials

Python app gets credentials for:
- **Notion** — single database (not full workspace; `NOTION_DATABASE_ID` scopes the token)
- **LLM proxy** — primary LLM calls only (`LLM_PROXY_API_KEY`)
- **OpenAI** — reward image generation only, optional (`OPENAI_API_KEY`)
- **Signal** — the owner's registered account (`SIGNAL_ACCOUNT`)

No home automation credentials, no general web access, no shell tool. LangSmith tracing disabled by default; startup guard refuses to boot if `LANGSMITH_TRACING=true` without explicit `ALLOW_PRIVATE_TRACE_EXPORT=true` consent.

### Accepted boundary

If prompt injection occurs, the blast radius is limited to what the Python app container can reach: Notion database writes, Signal messages to the owner's number, and Anthropic/OpenAI API calls within normal usage. Host admin surfaces require Tailscale. Postgres credentials are Docker-internal only — not accessible from outside the compose network. If container compromise occurs, incident response is container restart after rotating Notion API key and SIGNAL_ACCOUNT credentials — no persistent host state is owned by the app outside Docker volumes.

### Network isolation

VM is KVM virtual machine on home network, not personal laptop or shared system. VLAN segmentation isolates from internal network — router blocks internal IP ranges from the app VLAN.

App admin interfaces (SSH, control UI) require Tailscale overlay with device posture checks. On host, UFW defaults deny-all inbound with explicit allows for SSH and WireGuard only. Outbound unrestricted at host level — host-level services like Tailscale require unrestricted egress. App runs in Docker containers; outbound HTTP/HTTPS from containers routed through forward proxy via container's proxy environment variables. Kernel-level egress rules enforce this independently of container environment — even if a process unsets proxy variables, direct internet traffic dropped.

### Forward proxy

Squid proxy enforces domain allowlist. Agent (or anything in the app containers) trying to reach non-allowlisted domain — connection denied. Mitigates prompt injection: agent won't consume arbitrary web content. The constrained tool surface (no `fetch_url`) reinforces this at the application layer.

**Caveat:** Kernel-level egress rules (see [Network isolation](#network-isolation)) enforce allowlist independently of container environment — modifying or unsetting proxy variables does not bypass restriction.

Proxy also blocks connections to private network ranges (RFC 1918, loopback, link-local, overlay subnet) to prevent DNS rebinding. Caching disabled, `forwarded_for` headers stripped, version string suppressed.

### Inbound exposure reduction

- Signal WebSocket ingress is the only inbound path to the Python app
- Signal authentication: only the registered account owner can send messages
- No exposed HTTP/HTTPS listener on the host for the main app
- APScheduler jobs (reminder delivery, health checks, ops alerts drain) are outbound-only — no inbound trigger surface

### Configuration hardening

- Host dedicated to this stack; no other services hosted
- API keys in `.env` (gitignored); never committed, never logged by the app
- `reward_manifests` Postgres table stores task titles (private column) — never logged or committed
- LangSmith guard: app refuses to boot when `LANGSMITH_TRACING=true` unless `ALLOW_PRIVATE_TRACE_EXPORT=true` set (see `app/main.py`)

## Threat model

| Threat | Trust model | Mitigation |
|--------|-------------|------------|
| Prompt injection via user message | App is [BC] for direct interaction — Signal authenticated/paired, only owner can send messages | Low risk; owner is only input source |
| Prompt injection via GitHub content | CI review agents are [AC] — no infrastructure credentials or Notion access | Fork PRs blocked from self-hosted runners; devcontainer built only from main; self-hosted runners isolated by VLAN segmentation |
| Agent reaches arbitrary web content | Python app has no `fetch_url`, no shell tool | Constrained tool surface + proxy allowlist + kernel-level egress rules |
| Agent pivots to internal network | Constrained tool surface; no shell | VLAN segmentation blocks internal network at router; proxy blocks private ranges; kernel-level egress rules enforce independently of container environment |
| Malicious PR manipulates review agent | Review agents are [AC] — no access to secrets or infrastructure | Fork PRs blocked; security gate jobs on ubuntu-latest; runners isolated by VLAN |
| Credential exfiltration via prompt injection | App has credentials in runtime context, could be prompted to reveal them | Proxy allowlist limits where credentials could be sent; constrained tool surface (no URL fetch, no shell); model alignment is speed bump, not guarantee |
| Unauthorized admin access | Admin interfaces require Tailscale authentication | Firewall allows only SSH and WireGuard inbound |

## Reporting vulnerabilities

Security issue found — report via [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). No public issues.

Acknowledge within 48 hours.
