# llm-live-preview

A live-reloading browser preview for [llm](https://github.com/simonw/llm) conversations, with full Markdown and LaTeX rendering via pandoc and MathJax.

Every time you send a prompt, the browser tab updates automatically with the full conversation — both your messages and the model's responses.

## Workflow

**1. Start the preview server** (opens browser automatically):
```sh
python3 preview.py
```

**2. Send prompts** using `llm-conv` instead of `llm`:
```sh
./llm-conv "Derive the Euler-Lagrange equation"
./llm-conv -c "Now show the derivation for a pendulum"   # continue conversation
./llm-conv -m gpt-4o "Explain Fourier transforms"        # pick a model
./llm-conv chat                                          # interactive chat session
```

In prompt mode, the response streams live to the browser as it's generated. After completion, the file is rewritten from the conversation log so your prompts appear too. In chat mode, the browser updates every 2 seconds as new turns complete.

## Requirements

- [`llm`](https://llm.datasette.io/) with logging enabled (on by default)
- `pandoc`
- Python 3.11+
