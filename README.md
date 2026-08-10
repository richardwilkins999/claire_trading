# claire_trading

Claire — a LangGraph multi-agent paper-trading desk. Specialist LLM agents
research a stock, a bull and a bear debate it, an arbiter issues a typed
verdict, and nothing executes without explicit human approval verified by a
single-use token. Execution and accounting are deterministic Python — no LLM
ever touches money.

**Start here: [DESIGN.md](DESIGN.md)** — the complete build-from-scratch
specification (architecture, graph, schemas, provider layer, approval flow,
lessons learnt, build order). Code lands in this repo following its §20 build
order; deploy target is `/opt/Claire`.

Paper/simulation trading only. Single user, localhost only.
