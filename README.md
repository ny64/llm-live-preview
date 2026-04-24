# llm-live-preview

A live-reloading browser preview for [llm](https://github.com/simonw/llm) conversations, with full Markdown and LaTeX rendering via pandoc and MathJax.
The browser updates automatically as responses stream in, showing both your prompts and the model's replies.

## Workflow

**1. Start the preview server** (opens a browser tab automatically):
```sh
python3 preview.py
```

**2. Chat from the browser** — type in the input bar at the bottom and hit Ctrl+Enter (or Send). Use **New** to start a fresh conversation.

**Or use the terminal** with `llm-conv` instead of `llm`:
```sh
./llm-conv "Derive the Euler-Lagrange equation"
./llm-conv -c "Now show the derivation for a pendulum"   # continue conversation
./llm-conv -m gpt-4o "Explain Fourier transforms"        # pick a model
./llm-conv chat                                          # interactive chat session
```

In prompt mode the response streams live to the browser. In chat mode the browser updates every 2 seconds as turns complete.

## Requirements

- [`llm`](https://llm.datasette.io/) with logging enabled (on by default)
- `pandoc`
- Python 3.11+
