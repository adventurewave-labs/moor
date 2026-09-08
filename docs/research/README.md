# Research: Why configuration drift, and why compose

This directory contains the research base behind Moor. The raw web-search
result sets are preserved in [`raw/`](raw/); this file synthesizes the findings
and the sources they came from.

## The problem selected

Of the recurring "most common DevOps infrastructure problems" — config drift,
secrets sprawl, tool sprawl, CI/CD flakiness — **configuration drift** was
selected because it has the strongest third-party evidence linking it to
real production outages and cost, and because its dominant existing solutions
(Terraform drift detection, Kubernetes controllers) leave a large, measurable
gap: the docker-compose long tail.

> **Configuration drift** = divergence between the declared (desired) state of
> your infrastructure and its actual runtime state. A hotfix run by hand, a
> replica scaled from a console, an env var edited "just for today," a rogue
> `docker run` — each is a small, silent mutation that no declaration knows
> about, accumulating until the next deployment, failover, or audit fails.

## Key statistics and their sources

| Finding | Source |
|---|---|
| ~40% of organizations suffered a major outage caused by human error in the last three years; 85% of those stem from staff failing to follow procedures or follow them adequately | [Uptime Institute, Annual Outage Analysis 2025](https://uptimeinstitute.com/uptime-announces-annual-outage-analysis-report-2025) |
| 57% of respondents say their most recent major outage cost more than $100,000 | [Uptime Intelligence, Annual Outage Analysis 2026](https://intelligence.uptimeinstitute.com/annual-outage-analysis-2026) |
| Gartner projects ~80% of outages impacting mission-critical services will be caused by people and process issues, not technology | [Gartner, via OpsTrails analysis](https://www.opstrails.dev/) (RAS Core Research Note) |
| 90%+ of mid-size and large enterprises report a single hour of downtime costs >$300,000; 41% report $1M–$5M+ per hour | [ITIC 2024 Hourly Cost of Downtime](https://itic-corp.com/reports-survey-results/) |
| Docker is the single most-used tool among professional developers (59%) | [Stack Overflow Developer Survey 2024](https://survey.stackoverflow.co/2024/technology) |
| Flexible infrastructure and reliability practices directly drive organizational performance | [DORA, Accelerate State of DevOps Report 2024](https://dora.dev/research/2024/dora-report/) |
| Unmanaged drift directly causes outages, security gaps, prolonged dev cycles, and increased ticket volume | [Octopus Deploy — Configuration Drift](https://octopus.com/devops/configuration-management/configuration-drift), [Puppet — Configuration Drift](https://www.puppet.com/blog/configuration-drift) |

Read together, these paint a consistent picture: the majority of
mission-critical outages originate in **people and process** — manual changes
that diverge from declared state — and every hour of the resulting downtime
costs most organizations more than $300K.

## The gap in existing tooling

- **Terraform-family drift detection** (Terraform Cloud, Spacelift, env0,
  driftctl-style OSS) covers cloud *resources*, not the containers running on
  your hosts, and typically runs on a schedule of hours, not seconds
  ([drift tooling landscape](https://safeguard.sh/), [Scalr](https://scalr.com/)).
- **Kubernetes** solved runtime reconciliation properly (controllers +
  desired-state API), but k8s adoption remains a minority of teams and is
  heavyweight for the majority of real deployments.
- **docker-compose** — used by a majority of professional developers (59%,
  Stack Overflow 2024) — has **no reconciliation at all**: nothing watches
  whether running containers still match the compose file. Every compose
  deployment runs unguarded.
- Agentless CM tools (Ansible et al.) are criticized by practitioners
  precisely for enabling drift between runs
  ([r/sysadmin discussion](https://www.reddit.com/r/sysadmin/)).

**Moor's thesis:** bring the Kubernetes-style observe → diff → plan → act
control loop to docker-compose environments — the most widely deployed, least
guarded runtime in the industry — with second-scale detection, Slack-format
alerting, and opt-in auto-remediation.

## Contents

```
README.md        this synthesis
raw/*.json       the original search-result sets, per topic:
                 uptime.json        outage causes + cost (Uptime Institute)
                 gartner.json       Gartner outage/analysis coverage
                 downtime_cost.json ITIC hourly downtime cost 2024
                 dora.json          DORA State of DevOps 2024
                 compose_usage.json Docker/compose adoption (Stack Overflow)
                 drift_overview.json  drift definitions, causes, risks
                 parity.json        environment parity / repro gap
                 tools.json         existing drift-detection tooling
                 puppet.json        CM tool drift commentary
                 snowflake.json     cost-drift analogue (context)
```

The full product specification — problem framing, personas, functional and
non-functional requirements, competitive matrix, risks, and roadmap built on
this research — is [`../Moor_PRD.pdf`](../Moor_PRD.pdf).
