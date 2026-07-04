# Trinity 🎓

A voice-powered AI study assistant built for my two children 
(ages 7 and 10) who attend a Polish school while learning 
Greek at home.

## The Problem

When we moved to Poland, my kids were suddenly immersed in 
a language they didn't know at all. Today they learn Polish 
by ear and attend school in Polish — while also studying 
Greek at home. Trinity was built to help them study 
independently, without always waiting for me.

## What Trinity Does

- Listens to the child speak in Greek (voice input)
- Reads the relevant pages from their school books (PDF)
- Guides them with questions — never gives ready answers
- Remembers where they left off (session memory)
- Adapts to each child's age and curriculum

## Tech Stack

- **Claude API** (Anthropic) — AI reasoning & pedagogical guidance
- **OpenAI Whisper** — Greek speech-to-text
- **OpenAI TTS** — Voice responses
- **PyMuPDF** — PDF school book reading
- **Tkinter** — Custom animated UI with glowing orb effect
- **Pygame** — Audio playback
- **Python** — Everything else

## Architecture

- Config-driven (`config.json`) — easy to adapt for new children/languages
- Session memory system (JSON) — remembers progress between sessions
- PIN-protected admin controls
- Book context injection via Claude function-calling pattern
- Page cache to reduce API calls

## Setup

1. Clone the repository
2. Install dependencies:
   \`\`\`bash
   pip install anthropic openai sounddevice pymupdf pygame
   \`\`\`
3. Create `config.json` (see `config.example.json`)
4. Add your school books as PDFs in `books/` folder
5. Run:
   \`\`\`bash
   python trinity.py
   \`\`\`

## Configuration

Copy `config.example.json` to `config.json` and fill in:
- Anthropic API key
- OpenAI API key  
- Children profiles (name, books folder, age)
- Voice preference
- Admin PIN

## Project Structure

\`\`\`
TRINITY/
├── trinity.py          # Main application
├── config.json         # Your config (not in repo)
├── config.example.json # Template
├── books/              # School PDFs (not in repo)
│   ├── Child1/
│   └── Child2/
└── memory/             # Session memory (not in repo)
\`\`\`

## Notes

This is a personal project built for a specific family need.
It is not a product. It is proof that AI can solve real, 
human problems — not just enterprise ones.

---
Built by George | Senior AR Analyst | Python & AI enthusiast