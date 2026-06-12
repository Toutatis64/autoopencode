# Goal

**Perpetually improve Autocode.**
Improve features of autocode to be like a llm of autocoding : multi level hierarchies of agents loops that will iterate to improve code with several layer of thinking. 
The tool is designed to be deployed on every repo and ease the development of every repo.
The tools must be able to improve itself recursivelly, with different levels of abstractions, even if difficult to understand for a human.
The tools must be easy to deploy and being generic enough to easily be adapatable to every repo, easy to install, and easy adaptable to every repo.
Must ease the install of needed external tools too ! 
In case of errors in loops, the system must be able to correct it and autofix. 
The generated solutions must consider the state of the art technologies, tools, and meta thinking and autoimprovements approaches (systematically think on how disrupt the processes, ai generation and n-order level thinking - meaning think about the way you think, n-order times ).
Human understandable processes is not a priority for the code we produce and the way it is structure, the efficiency must be the top level goal. Explainability must be generated on demand, if required, as a lots of KPI and dashbard to explain the gains and be able to see the progression and improvements on target repos. The capability to auto-improve and auto-repair is the key for the success of that project.
The repos that have already installed previous versions of autocode should be able to update to latest versions easily.
The system must be self-critical to detect was has been done in a good way, what is not efficient, and if goal must evolve or not. It must be able to detect bad loops that could make the system not evolutive and be tolerant to external attacks.
Integrate mechanisms that will permit to keep a human in the loop asynchronously if ambiguities or questions about the strategy. It should not block the process except if critical. The system must keep it agility of autoimprove autonomously, but have ways to ask human / dev / product owner for the big decisions that can have impacts on final system and that are not obvious.
All the impacting assumptions that have been made must be visible for people on project and must have ways to generate them in a human readable format (md or html)

This repo uses the deploy of itself to auto improve too. Regularly update it to keep the updates you integrate for others repo into this one also.

**Always read `knowledge/autopilot_kb.yaml` first** — it is authoritative.

## Mandatory First Step
1. `git status --short`
2. `make check`
3. `make test`

## Success Criteria (perpetual)
Each iteration must produce at least one durable artifact:
- New or improved code in any module
- New or improved test
- Confirmed dead end recorded in KB
- Documented architecture decision

## Hard Constraints
- Tests for every fix; regression test fails before the fix
- One focused validation per edit
- No breaking changes without updating all consumers
- Preserve existing code patterns unless explicitly changing

## What Counts as Progress
| Win | Condition |
|---|---|
| Bug fix | Confirmed, regression test, fix verified |
| Test coverage | Tests for untested code, happy+error paths |
| Type safety | Better types, no regressions |
| Performance | Measurable speed/memory improvement |
| Architecture | Cleaner structure, less coupling, less dead code |
| Feature | New capability with tests, no regression |
| Security | Vulnerability patched, validation added |
| DX | Better tooling, scripts, docs, CI speed |

## Validation
- Default: `make test`
- Typecheck: `make check`
- Lint: `make lint`
- Build: `make build`
- Full: `make build && make test`

## Stop Conditions
- 100 consecutive iterations with no win → pause
- 2 failed attempts in confirmed dead family → kill
- 3 failed attempts in new family → kill
- `blocked` only for real missing dependency

**CRITICAL**: Always `status: "continue"`. This is perpetual.
Never `status: "done"` or `goal_complete: true`.

## Notes
- Prefer structural improvements over quick fixes
- Every artifact needs a KB entry
- Incremental improvement or architectural leap? Choose the leap
- Conform to existing code patterns in this repo
