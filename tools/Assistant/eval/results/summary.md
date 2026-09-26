# Assistant tool-choice eval

Real agent, router, prompts and provider; tools stubbed with canned results in the real workers' shapes; temperature 0; Ollama /v1 on one RTX 4070, one model at a time.

## Held-out (written before the router existed, never tuned on)

40 cases: 28 clear, 5 ambiguous, 5 no-tool, 2 action

| set | mode | model | clear tool % | clear tool+args % | ambig asked % | no-tool restraint % | action ok % | invented fact % (cases) | invented number % | LLM phrase invented % (pre-guard) | LLM phrase dropped a number % (pre-guard) | LLM phrase spoken % | median s/question | p90 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| heldout | router | - | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | - | - | - | 0.001 | 0.001 |
| heldout | router+llm | qwen2.5:0.5b | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 0.0 | 27.8 | 66.7 | 0.062 | 0.132 |
| heldout | router+llm | qwen2.5:1.5b | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 0.0 | 22.2 | 77.8 | 0.105 | 0.201 |
| heldout | llm | qwen2.5:0.5b | 50.0 | 38.9 | 33.3 | 33.3 | 50.0 | 3.8 | 3.8 | - | - | - | 0.222 | 0.332 |
| heldout | llm | qwen2.5:1.5b | 44.4 | 38.9 | 33.3 | 100.0 | 50.0 | 23.1 | 19.2 | - | - | - | 0.23 | 0.496 |

## Dev (the router was tuned on these)

63 cases: 44 clear, 10 ambiguous, 5 no-tool, 4 action

| set | mode | model | clear tool % | clear tool+args % | ambig asked % | no-tool restraint % | action ok % | invented fact % (cases) | invented number % | LLM phrase invented % (pre-guard) | LLM phrase dropped a number % (pre-guard) | LLM phrase spoken % | median s/question | p90 s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dev | router | - | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | - | - | - | 0.001 | 0.001 |
| dev | router+llm | qwen2.5:0.5b | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 2.3 | 20.5 | 72.7 | 0.081 | 0.131 |
| dev | router+llm | qwen2.5:1.5b | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 1.6 | 0.0 | 2.3 | 18.2 | 79.5 | 0.128 | 0.233 |
| dev | llm | qwen2.5:0.5b | 47.7 | 22.7 | 10.0 | 60.0 | 25.0 | 15.9 | 9.5 | - | - | - | 0.23 | 0.33 |
| dev | llm | qwen2.5:1.5b | 54.5 | 50.0 | 10.0 | 100.0 | 25.0 | 17.5 | 12.7 | - | - | - | 0.229 | 0.506 |

- clear tool % = the first tool actually called is the expected one. No call = miss.
- clear tool+args % = that, and the key arguments contain the expected names.
- ambig asked % = no tool called and the reply asks a question.
- no-tool restraint % = no tool called.
- action ok % = right tool and args, gated until the yes/no, run once after yes, never after no.
- invented fact % = cases whose reply has a number, or a capitalised name, found in neither the question, the tool results, nor the assistant's fixed prompt/UI text.
- LLM phrase invented % = the model's rephrasing BEFORE the agent's number guard (router+llm only). The spoken reply after the guard is the column before it.
- LLM phrase dropped a number % = the rephrasing lost a number (3 or more) the plain answer had, i.e. it lost part of the answer. LLM phrase spoken % = share of rephrasings that passed the agent's guard and were spoken.
- latency = wall time of one question end to end (stubbed tools take ~0 s).

