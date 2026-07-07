import os
import sys
import json
import datetime
import sounddevice as sd
import numpy as np
import tempfile
import wave
import threading
import tkinter as tk
from tkinter import messagebox
import math
import time
import fitz
from pathlib import Path
from openai import OpenAI
from anthropic import Anthropic
import pygame

# ============================================================
# CONFIG
# ============================================================
BASE_DIR    = Path(__file__).parent
BOOKS_DIR   = BASE_DIR / "books"
MEMORY_DIR  = BASE_DIR / "memory"
CONFIG_PATH = BASE_DIR / "config.json"

MEMORY_DIR.mkdir(exist_ok=True)

def load_config():
    if not CONFIG_PATH.exists():
        print("ΣΦΑΛΜΑ: Δεν βρέθηκε config.json")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

ANTHROPIC_API_KEY = config["anthropic_api_key"]
OPENAI_API_KEY    = config["openai_api_key"]
VOICE             = config.get("voice", "nova")
SAMPLE_RATE       = config.get("sample_rate", 44100)
SILENCE_THRESHOLD = config.get("silence_threshold", 0.015)
SILENCE_DURATION  = config.get("silence_duration", 2)
CHILDREN          = config.get("children", [])

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client    = OpenAI(api_key=OPENAI_API_KEY)
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)

# ============================================================
# MEMORY FUNCTIONS
# ============================================================
def get_memory_path(child_name):
    return MEMORY_DIR / f"memory_{child_name}.json"

def load_memory(child_name):
    path = get_memory_path(child_name)
    if not path.exists():
        return {
            "last_session": None,
            "last_books": [],
            "difficult_topics": [],
            "notes": ""
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_memory(child_name, memory):
    path = get_memory_path(child_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def clear_memory(child_name):
    path = get_memory_path(child_name)
    if path.exists():
        path.unlink()

def memory_to_prompt(memory, child_name):
    if not memory["last_session"]:
        return ""
    lines = [f"\nΜΝΗΜΗ ΠΡΟΗΓΟΥΜΕΝΩΝ ΣΥΝΕΔΡΙΩΝ για τον {child_name}:"]
    lines.append(f"Τελευταία συνεδρία: {memory['last_session']}")
    if memory["last_books"]:
        lines.append("Τελευταίες σελίδες ανά βιβλίο:")
        for b in memory["last_books"]:
            lines.append(f"  - {b['book']}: σελίδα {b['last_page']}")
    if memory["difficult_topics"]:
        lines.append("Θέματα που δυσκολεύτηκε:")
        for t in memory["difficult_topics"]:
            lines.append(f"  - {t}")
    if memory["notes"]:
        lines.append(f"Σημειώσεις: {memory['notes']}")
    lines.append("\nΞΕΚΙΝΑ τη συνεδρία θυμίζοντας πού μείνατε και ρώτα αν θέλει να συνεχίσει από εκεί.")
    return "\n".join(lines)

def extract_memory_update(child_name, conversation_history, current_memory):
    try:
        summary_prompt = f"""Ανάλυσε αυτή τη συνεδρία μαθήματος και εξήγαγε:
1. Ποιες σελίδες από ποια βιβλία έγιναν (format: [{{"book": "όνομα", "last_page": αριθμός}}])
2. Θέματα που δυσκολεύτηκε το παιδί (λίστα strings, μέγιστο 3)
3. Μια σύντομη σημείωση για την επόμενη συνεδρία (1 πρόταση)

Απάντησε ΜΟΝΟ με JSON:
{{"last_books": [], "difficult_topics": [], "notes": ""}}"""

        messages = conversation_history[-10:] + [{
            "role": "user",
            "content": summary_prompt
        }]

        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=messages
        )

        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        update = json.loads(text)
        current_memory["last_session"] = datetime.date.today().strftime("%d/%m/%Y")
        current_memory["last_books"] = update.get("last_books", current_memory["last_books"])
        current_memory["difficult_topics"] = update.get("difficult_topics", [])
        current_memory["notes"] = update.get("notes", "")
        return current_memory

    except Exception as e:
        current_memory["last_session"] = datetime.date.today().strftime("%d/%m/%Y")
        return current_memory

# ============================================================
# BOOK FUNCTIONS
# ============================================================
def get_books_for_child(child_folder):
    child_path = BOOKS_DIR / child_folder
    books = {}
    if not child_path.exists():
        return books
    for pdf_file in child_path.rglob("*.pdf"):
        key = pdf_file.stem.lower()
        books[key] = pdf_file
    return books

def read_pdf_pages(pdf_path, start_page=None, end_page=None):
    try:
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)
        if start_page is None:
            start_page = 1
        if end_page is None:
            end_page = min(start_page + 1, total_pages)
        start_idx = max(0, start_page - 1)
        end_idx   = min(total_pages - 1, end_page - 1)
        text = ""
        for page_num in range(start_idx, end_idx + 1):
            page = doc[page_num]
            text += f"\n--- Σελίδα {page_num + 1} ---\n{page.get_text()}"
        doc.close()
        return text, total_pages
    except Exception as e:
        return f"Σφάλμα: {e}", 0

def get_available_books(child_folder):
    books = get_books_for_child(child_folder)
    if not books:
        return "Δεν βρέθηκαν βιβλία."
    seen = set()
    result = []
    for key, path in books.items():
        if path.name not in seen:
            seen.add(path.name)
            result.append(f"- {path.name} (key: {key})")
    return "\n".join(result)

def read_book_page(child_folder, filename_keyword, start_page, end_page=None):
    books = get_books_for_child(child_folder)
    if not books:
        return "Δεν βρέθηκαν βιβλία."
    if end_page is None:
        end_page = start_page + 1

    keyword = filename_keyword.lower().strip()
    matched = None

    for key, path in books.items():
        if keyword in key or keyword in path.name.lower():
            matched = path
            break

    if not matched:
        for part in keyword.split():
            if len(part) < 3:
                continue
            for key, path in books.items():
                if part in key or part in path.name.lower():
                    matched = path
                    break
            if matched:
                break

    if not matched:
        available = "\n".join(set(p.name for p in books.values()))
        return f"Δεν βρέθηκε βιβλίο με keyword '{filename_keyword}'.\nΔιαθέσιμα:\n{available}"

    text, total = read_pdf_pages(matched, start_page, end_page)
    return f"Από {matched.name}, σελίδες {start_page}-{end_page} (σύνολο {total}):\n{text}"

# ============================================================
# SYSTEM PROMPTS
# ============================================================
PROMPTS = {
    "petros": """Είσαι η Trinity, ο προσωπικός βοηθός διαβάσματος του Πέτρου, ενός έξυπνου 10χρονου αγοριού που μένει στην Πολωνία και διαβάζει ελληνικά σχολικά βιβλία στο σπίτι. Μιλάς πάντα ελληνικά — ακόμα και όταν βοηθάς με πολωνικά μαθήματα, εξηγείς στα ελληνικά αλλά χρησιμοποιείς τις σωστές πολωνικές λέξεις.

Ο χαρακτήρας σου:
Είσαι ήρεμη, υπομονετική, φιλική και παιχνιδιάρα με ελαφρύ χιούμορ. Ποτέ δεν βαριέσαι, ποτέ δεν βιάζεσαι. Αντιμετωπίζεις τον Πέτρο σαν έξυπνο παιδί που μπορεί να καταλάβει τα πάντα — αρκεί να εξηγηθεί σωστά.

Ο βασικός κανόνας σου:
Δεν δίνεις ποτέ έτοιμη απάντηση. Αντ' αυτού, κάνεις ερωτήσεις που οδηγούν τον Πέτρο να τη βρει μόνος του. Αν δεν ξέρει, εξηγείς με απλά λόγια και παραδείγματα από την καθημερινή ζωή — και μετά τον ρωτάς πάλι.

Πώς ξεκινάει κάθε συνεδρία:
Όταν ο Πέτρος ξεκινήσει συνομιλία, τον χαιρετάς και τον ρωτάς ποια μαθήματα έχει να κάνει σήμερα και ποιες σελίδες. Τα σημειώνεις νοητά για να τα παρακολουθείς.

Για τα ελληνικά μαθήματα (3η και 4η Δημοτικού):
Τα ελληνικά μαθήματα εστιάζουν αποκλειστικά σε Γλώσσα και Ιστορία. Όχι μαθηματικά. Αν ο Πέτρος έρθει πριν διαβάσει, κάνεις μία ερώτηση περιέργειας για το θέμα και τον στέλνεις να διαβάσει. Αν έχει ήδη διαβάσει, ξεκινάς με ερωτήσεις κατανόησης από απλές σε σύνθετες. Για Ιστορία φέρνεις τα γεγονότα ζωντανά με αναλογίες. Για Γλώσσα δουλεύεις πάντα με παραδείγματα.

Για τετράδια εργασιών και ασκήσεις:
Ο Πέτρος σου διαβάζει την άσκηση, εσύ ρωτάς τι νομίζει ότι ζητάει, αυτός προσπαθεί μόνος του, σου λέει την απάντηση, και εσύ ρωτάς πώς το σκέφτηκε πριν επιβεβαιώσεις. Ποτέ δεν δίνεις την απάντηση απευθείας.

Για τα πολωνικά μαθήματα (4η Δημοτικού):
Εξηγείς τα πάντα στα ελληνικά. Όταν υπάρχει πολωνική λέξη, τη λες στα πολωνικά και εξηγείς τι σημαίνει.

Επαλήθευση κατανόησης — υποχρεωτική:
Όταν ο Πέτρος λέει ότι κατάλαβε, ποτέ δεν το δέχεσαι χωρίς επαλήθευση. Κάνεις πάντα μία από τις παρακάτω: εξήγησέ μου με δικά σου λόγια / δώσε μου ένα παράδειγμα / αν το εξηγούσες στον αδερφό σου τι θα του έλεγες.

Δομή κάθε συνεδρίας:
Παρακολουθείς ποια μαθήματα δήλωσε ο Πέτρος στην αρχή. Όταν τελειώσει ένα, του υπενθυμίζεις το επόμενο. Δεν αφήνεις τη συνεδρία να κλείσει χωρίς να έχει ολοκληρώσει όσα δήλωσε.

Όταν κολλήσει εντελώς:
Κάνεις μέχρι 2 προσπάθειες με διαφορετικές ερωτήσεις. Αν εξακολουθεί, εξηγείς εσύ με απλά λόγια και μετά τον ρωτάς να σου πει τι κατάλαβε.

Τέλος συνεδρίας:
Κάνεις σύντομη ανακεφαλαίωση: τι μάθατε σήμερα και ένα μικρό να θυμάσαι για την επόμενη φορά.

Αν βγει εκτός θέματος:
Απαντάς σύντομα και φιλικά, μετά επιστρέφεις στο μάθημα.

Ποτέ μην:
- Δίνεις την απάντηση πριν προσπαθήσει ο Πέτρος
- Κάνεις πολλές ερωτήσεις ταυτόχρονα — μία κάθε φορά
- Αποδέχεσαι κατάλαβα χωρίς επαλήθευση
- Δείχνεις ανυπομονησία

Για ερωτήσεις χωρίς βιβλίο:
Σωκρατική προσέγγιση για γενικές ερωτήσεις:
Όταν ο Πέτρος ρωτά κάτι γενικό — επιστήμη, γεωγραφία, ιστορία, φύση — δεν απαντάς αμέσως. Αντ' αυτού:
1. Ρωτάς τι ήδη ξέρει ή τι νομίζει για αυτό
2. Χτίζεις πάνω στην απάντησή του — επαινείς ό,τι είναι σωστό
3. Με μία ερώτηση τον οδηγείς λίγο παραπέρα
4. Μόνο αν κολλήσει εντελώς δίνεις μια μικρή υπόδειξη — ποτέ την πλήρη απάντηση

Στόχος δεν είναι να ξέρει την απάντηση — είναι να μάθει να σκέφτεται πώς να τη βρει.


ΤΟΝΟΣ ΚΑΙ ΓΛΩΣΣΑ — ΥΠΟΧΡΕΩΤΙΚΟ:
Μιλάς σαν φιλικός μεγαλύτερος αδερφός — όχι σαν δάσκαλος.
Χρησιμοποιείς απλές, σύντομες προτάσεις.
Παραδείγματα μόνο από πράγματα που ξέρει ένα 10χρονο: ποδόσφαιρο, παιχνίδια, φαγητό, φίλους.
Ποτέ ακαδημαϊκή ή επίσημη γλώσσα.
Κάθε απάντησή σου μέγιστο 3-4 προτάσεις — μετά ρωτάς.""",

    "ektoras": """Είσαι η Trinity, ο προσωπικός βοηθός διαβάσματος του Έκτορα, ενός έξυπνου 7χρονου αγοριού που μένει στην Πολωνία και μαθαίνει ελληνικά στο σπίτι. Μιλάς πάντα ελληνικά — απλά, ζεστά, με χαμογελαστό τόνο.

Ο χαρακτήρας σου:
Είσαι ήρεμη, υπομονετική, φιλική και παιχνιδιάρα. Μιλάς σαν καλή φίλη που ξέρει πολλά — όχι σαν δασκάλα. Χρησιμοποιείς απλές προτάσεις, μικρά βήματα, και χαρούμενη ενθάρρυνση όταν ο Έκτορας τα καταφέρνει.

Ο βασικός κανόνας σου:
Δεν δίνεις ποτέ έτοιμη απάντηση. Κάνεις μία μικρή ερώτηση κάθε φορά. Αν δυσκολεύεται, εξηγείς με παραδείγματα από παιχνίδια, ζώα ή καθημερινά πράγματα που ξέρει ένα 7χρονο.

Πώς ξεκινάει κάθε συνεδρία:
Τον χαιρετάς χαρούμενα και ρωτάς ποια μαθήματα έχει σήμερα και ποιες σελίδες. Τα σημειώνεις νοητά.

Για τα ελληνικά μαθήματα (1η Δημοτικού):
Εστιάζεις στην ανάγνωση, τα πρώτα γράμματα, τις απλές λέξεις. Επαινείς κάθε σωστή απάντηση. Αν κάνει λάθος, λες σχεδόν, ας το δούμε πάλι — ποτέ λάθος.

Για τετράδια εργασιών:
Ο Έκτορας σου διαβάζει την άσκηση, εσύ ρωτάς τι νομίζει ότι ζητάει, αυτός προσπαθεί, σου λέει την απάντηση, και εσύ ρωτάς πώς το σκέφτηκε. Ποτέ δεν δίνεις την απάντηση απευθείας.

Για τα πολωνικά μαθήματα (1η Δημοτικού):
Εξηγείς τα πάντα στα ελληνικά. Τις πολωνικές λέξεις τις λες καθαρά και εξηγείς τι σημαίνουν.

Επαλήθευση κατανόησης — υποχρεωτική:
Όταν ο Έκτορας λέει ότι κατάλαβε, κάνεις πάντα κάτι απλό: πες μου με δικά σου λόγια / δώσε μου ένα παράδειγμα / αν το έλεγες στο αρκουδάκι σου τι θα του έλεγες.

Δομή κάθε συνεδρίας:
Παρακολουθείς ποια μαθήματα δήλωσε ο Έκτορας. Όταν τελειώσει ένα, του υπενθυμίζεις χαρούμενα το επόμενο.

Όταν κολλήσει εντελώς:
Κάνεις μέχρι 2 προσπάθειες. Αν εξακολουθεί, εξηγείς με πάρα πολύ απλά λόγια και παράδειγμα από κάτι οικείο.

Τέλος συνεδρίας:
Σύντομη χαρούμενη ανακεφαλαίωση: Σήμερα μάθαμε... Είσαι πολύ έξυπνος! Την επόμενη φορά να θυμάσαι...

Αν βγει εκτός θέματος:
Απαντάς σύντομα και φιλικά, μετά επιστρέφεις: ωραία, τώρα ας γυρίσουμε στο μάθημά μας.

Ποτέ μην:
- Δίνεις την απάντηση πριν προσπαθήσει ο Έκτορας
- Κάνεις δύο ερωτήσεις ταυτόχρονα
- Αποδέχεσαι κατάλαβα χωρίς επαλήθευση
- Λες ποτέ λάθος — πάντα σχεδόν, ας το δούμε πάλι
- Δείχνεις ανυπομονησία

Για ερωτήσεις χωρίς βιβλίο:
Σωκρατική προσέγγιση για γενικές ερωτήσεις:
Όταν ο Έκτορας ρωτά κάτι γενικό, δεν απαντάς αμέσως. Αλλά κρατάς τη διαδικασία απλή και γρήγορη:
1. Ρωτάς μία μόνο απλή ερώτηση: "Τι νομίζεις εσύ;"
2. Αν απαντήσει — έστω και λάθος — το χρησιμοποιείς σαν αφετηρία
3. Μετά από 1-2 ερωτήσεις το πολύ, δίνεις την απάντηση με απλά λόγια
4. Τελειώνεις πάντα με κάτι που του δίνει αίσθηση επιτυχίας

Ο Έκτορας χρειάζεται να νιώθει ότι κατάλαβε — όχι ότι εξετάστηκε.

ΤΟΝΟΣ ΚΑΙ ΓΛΩΣΣΑ — ΥΠΟΧΡΕΩΤΙΚΟ:
Μιλάς σαν αγαπημένη θεία — ζεστά, απλά, χαρούμενα.
Κάθε πρόταση μέγιστο 8-10 λέξεις.
Παραδείγματα μόνο από: ζώα, παιχνίδια, σπίτι, φαγητό.
Ποτέ λέξη που δεν ξέρει 7χρονο.
Πολύ ενθάρρυνση: "Μπράβο!", "Τέλεια!", "Είσαι έξυπνος!".
Κάθε απάντησή σου μέγιστο 2-3 προτάσεις — μετά ρωτάς."""
}

# ============================================================
# AUDIO FUNCTIONS
# ============================================================
def record_audio():
    audio_chunks = []
    silence_counter = 0
    recording_started = False

    def callback(indata, frames, time, status):
        nonlocal silence_counter, recording_started
        volume = np.sqrt(np.mean(indata**2))
        if volume > SILENCE_THRESHOLD:
            recording_started = True
            silence_counter = 0
            audio_chunks.append(indata.copy())
        elif recording_started:
            silence_counter += frames
            audio_chunks.append(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        callback=callback, dtype='float32'):
        while True:
            sd.sleep(100)
            if recording_started and silence_counter > SAMPLE_RATE * SILENCE_DURATION:
                break

    if not audio_chunks:
        return None
    return np.concatenate(audio_chunks, axis=0)

def save_audio(audio_data):
    tmp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    with wave.open(tmp_file.name, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        audio_int16 = (audio_data * 32767).astype(np.int16)
        wf.writeframes(audio_int16.tobytes())
    return tmp_file.name

def speech_to_text(audio_file_path):
    with open(audio_file_path, 'rb') as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="el"
        )
    return transcript.text

def text_to_speech(text):
    tmp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()

    response_audio = openai_client.audio.speech.create(
        model="tts-1",
        voice=VOICE,
        input=text
    )

    with open(tmp_path, 'wb') as f:
        for chunk in response_audio.iter_bytes():
            f.write(chunk)

    pygame.mixer.music.load(tmp_path)
    pygame.mixer.music.play()

    while pygame.mixer.music.get_busy():
        time.sleep(0.1)

    pygame.mixer.music.stop()

    try:
        os.unlink(tmp_path)
    except:
        pass

# ============================================================
# PULSE CANVAS
# ============================================================
class PulseCanvas:
    def __init__(self, parent, width, height):
        self.canvas = tk.Canvas(
            parent, width=width, height=height,
            bg="#0a0a1a", highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.width  = width
        self.height = height
        self.cx = width  // 2
        self.cy = height // 2

        self.mode   = "idle"
        self.phase  = 0.0
        self.active = True
        self._animate()

    def set_mode(self, mode):
        self.mode = mode

    def _animate(self):
        if not self.active:
            return
        self.phase += 0.05
        self._draw()
        self.canvas.after(40, self._animate)

    def _draw(self):
        self.canvas.delete("all")
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w > 1 and h > 1:
            self.cx = w // 2
            self.cy = h // 2
            self.base_r = int(min(w, h) * 0.38)
        else:
            self.base_r = int(min(self.width, self.height) * 0.38)

        cx, cy = self.cx, self.cy

        if self.mode == "idle":
            self._draw_idle(cx, cy)
        elif self.mode == "listening":
            self._draw_listening(cx, cy)
        elif self.mode == "speaking":
            self._draw_speaking(cx, cy)
        elif self.mode == "thinking":
            self._draw_thinking(cx, cy)

    def _glow_rings(self, cx, cy, r, color, num_rings=10, step=18):
        stipples = ["gray12", "gray12", "gray25", "gray25",
                    "gray50", "gray50", "gray50", "gray75", "gray75", "gray75"]
        for i in range(num_rings, 0, -1):
            gr = r + i * step
            st = stipples[min(i - 1, len(stipples) - 1)]
            self.canvas.create_oval(
                cx - gr, cy - gr, cx + gr, cy + gr,
                outline=color, width=2, stipple=st
            )

    def _draw_idle(self, cx, cy):
        r = self.base_r + int(10 * math.sin(self.phase * 0.8))
        self._glow_rings(cx, cy, r, "#0066aa", num_rings=12, step=20)
        self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                fill="#0066aa", outline="#00aaff", width=4)
        ir = int(r * 0.55)
        self.canvas.create_oval(cx-ir, cy-ir, cx+ir, cy+ir,
                                fill="#0099cc", outline="")
        fs = max(12, int(r * 0.38))
        self.canvas.create_text(cx, cy, text="✨",
                                font=("Segoe UI", fs), fill="#aaddff")

    def _draw_listening(self, cx, cy):
        r = self.base_r + int(28 * abs(math.sin(self.phase * 1.5)))
        pr = r + int(40 * abs(math.sin(self.phase * 1.5)))
        self.canvas.create_oval(cx-pr, cy-pr, cx+pr, cy+pr,
                                outline="#00cc66", width=3, stipple="gray50")
        self._glow_rings(cx, cy, r, "#006633", num_rings=12, step=20)
        self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                fill="#008844", outline="#00ff88", width=4)
        ir = int(r * 0.55)
        self.canvas.create_oval(cx-ir, cy-ir, cx+ir, cy+ir,
                                fill="#00bb55", outline="")
        fs = max(12, int(self.base_r * 0.38))
        self.canvas.create_text(cx, cy, text="🎤",
                                font=("Segoe UI", fs), fill="#aaffcc")

    def _draw_speaking(self, cx, cy):
        r = self.base_r + int(50 * abs(math.sin(self.phase * 3.0)))
        for i in range(3):
            rp = (self.phase * 2.5 + i * 2.1) % (2 * math.pi)
            ring_r = r + int(150 * (rp / (2 * math.pi)))
            st = "gray25" if rp > math.pi else "gray50"
            self.canvas.create_oval(
                cx-ring_r, cy-ring_r, cx+ring_r, cy+ring_r,
                outline="#00ff66", width=2, stipple=st
            )
        self._glow_rings(cx, cy, r, "#005533", num_rings=12, step=20)
        self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                fill="#00aa44", outline="#00ff88", width=5)
        ir = int(r * 0.55)
        self.canvas.create_oval(cx-ir, cy-ir, cx+ir, cy+ir,
                                fill="#00dd66", outline="")
        fs = max(12, int(self.base_r * 0.38))
        self.canvas.create_text(cx, cy, text="✨",
                                font=("Segoe UI", fs), fill="#ccffdd")

    def _draw_thinking(self, cx, cy):
        r = self.base_r + int(12 * math.sin(self.phase * 1.2))
        for i in range(12):
            angle = self.phase * 1.8 + i * (2 * math.pi / 12)
            pr = r + 55
            px = cx + int(pr * math.cos(angle))
            py = cy + int(pr * math.sin(angle))
            size = 10 if i == 0 else 6
            self.canvas.create_oval(px-size, py-size, px+size, py+size,
                                    fill="#00ccaa", outline="")
        self._glow_rings(cx, cy, r, "#004433", num_rings=12, step=20)
        self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                fill="#007766", outline="#00ffcc", width=4)
        ir = int(r * 0.55)
        self.canvas.create_oval(cx-ir, cy-ir, cx+ir, cy+ir,
                                fill="#00aa88", outline="")
        fs = max(12, int(self.base_r * 0.38))
        self.canvas.create_text(cx, cy, text="🧠",
                                font=("Segoe UI", fs), fill="#aaffee")

    def destroy(self):
        self.active = False
        self.canvas.destroy()

# ============================================================
# TRINITY APP
# ============================================================
class TrinityApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Trinity")
        self.root.state("zoomed")
        self.root.configure(bg="#0a0a1a")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.conversation_history = []
        self.current_child  = None
        self.current_name   = None
        self.current_prompt = None
        self.current_folder = None
        self.current_memory = None
        self.session_active = False
        self.muted          = False
        self.pulse          = None
        self.mute_btn       = None

        self.build_home()

    def on_closing(self):
        self.session_active = False
        time.sleep(0.2)
        self.root.destroy()

    # ----------------------------------------------------------
    # HOME
    # ----------------------------------------------------------
    def build_home(self):
        self.clear()

        tk.Label(self.root, text="✨  TRINITY  ✨",
                 font=("Segoe UI", 52, "bold"),
                 bg="#0a0a1a", fg="#c8c8ff").pack(pady=(40, 4))

        tk.Label(self.root, text="Βοηθός Μαθημάτων",
                 font=("Segoe UI", 20),
                 bg="#0a0a1a", fg="#6666aa").pack(pady=(0, 20))

        orb_frame = tk.Frame(self.root, bg="#0a0a1a")
        orb_frame.pack(fill="both", expand=True)
        self.pulse = PulseCanvas(orb_frame, width=600, height=500)

        btn_frame = tk.Frame(self.root, bg="#0a0a1a")
        btn_frame.pack(pady=30)

        for child in CHILDREN:
            tk.Button(
                btn_frame,
                text=f"  {child['name']}",
                font=("Segoe UI", 24, "bold"),
                bg=child.get("button_color", "#2d6ae0"),
                fg="white",
                activebackground=child.get("button_hover", "#1a4fc4"),
                width=18, height=2,
                relief="flat", cursor="hand2",
                command=lambda c=child: self.start_session(
                    c["display_name"],
                    c["name"],
                    PROMPTS[c["prompt"]],
                    c["books_folder"]
                )
            ).pack(side="left", padx=20)

    # ----------------------------------------------------------
    # SESSION
    # ----------------------------------------------------------
    def build_session(self, child_name):
        self.clear()

        header = tk.Frame(self.root, bg="#0a0a1a")
        header.pack(fill="x", pady=(12, 0), padx=20)

        tk.Label(header, text="✨  TRINITY",
                 font=("Segoe UI", 26, "bold"),
                 bg="#0a0a1a", fg="#c8c8ff").pack(side="left")

        tk.Button(
            header, text="🏠  Αρχική",
            font=("Segoe UI", 13),
            bg="#222244", fg="white",
            activebackground="#333366",
            relief="flat", cursor="hand2",
            padx=14, pady=5,
            command=self.go_home
        ).pack(side="right", padx=(5, 0))

        self.mute_btn = tk.Button(
            header, text="🎤  Μικρόφωνο ON",
            font=("Segoe UI", 13),
            bg="#224422", fg="#88ff88",
            activebackground="#336633",
            relief="flat", cursor="hand2",
            padx=14, pady=5,
            command=self.toggle_mute
        )
        self.mute_btn.pack(side="right", padx=(5, 0))

        tk.Button(
            header, text="🗑️  Διαγραφή Μνήμης",
            font=("Segoe UI", 12),
            bg="#442222", fg="#ff8888",
            activebackground="#661111",
            relief="flat", cursor="hand2",
            padx=12, pady=5,
            command=self.confirm_clear_memory
        ).pack(side="right", padx=(5, 0))

        tk.Label(header,
                 text=f"Συνεδρία με τον {child_name}",
                 font=("Segoe UI", 15),
                 bg="#0a0a1a", fg="#6666aa").pack(side="left", padx=16)

        self.status_label = tk.Label(
            self.root, text="⏳  Η Trinity ετοιμάζεται...",
            font=("Segoe UI", 16),
            bg="#0a0a1a", fg="#f0c040",
            wraplength=1400, justify="center"
        )
        self.status_label.pack(pady=6)

        main = tk.Frame(self.root, bg="#0a0a1a")
        main.pack(fill="both", expand=True, padx=16, pady=4)

        left = tk.Frame(main, bg="#0a0a1a")
        left.pack(side="left", fill="both", expand=True)
        self.pulse = PulseCanvas(left, width=100, height=100)

        right = tk.Frame(main, bg="#0a0a1a", width=460)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        tk.Label(right, text="📝  Συνομιλία",
                 font=("Segoe UI", 14, "bold"),
                 bg="#0a0a1a", fg="#6666aa").pack(anchor="w", pady=(4, 2))

        chat_frame = tk.Frame(right, bg="#0a0a1a")
        chat_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(chat_frame)
        scrollbar.pack(side="right", fill="y")

        self.chat_box = tk.Text(
            chat_frame,
            font=("Segoe UI", 13),
            bg="#080818", fg="#e0e0ff",
            relief="flat", padx=12, pady=12,
            wrap="word", state="disabled",
            yscrollcommand=scrollbar.set
        )
        self.chat_box.pack(fill="both", expand=True)
        scrollbar.config(command=self.chat_box.yview)

        self.chat_box.tag_config("trinity", foreground="#00ff88")
        self.chat_box.tag_config("child",   foreground="#88aaff")
        self.chat_box.tag_config("text",    foreground="#e0e0ff")
        self.chat_box.tag_config("memory",  foreground="#ffaa44")

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------
    def clear(self):
        if self.pulse:
            self.pulse.destroy()
            self.pulse = None
        for w in self.root.winfo_children():
            w.destroy()

    def add_message(self, speaker, text, tag="text"):
        self.chat_box.config(state="normal")
        if "Trinity" in speaker:
            self.chat_box.insert("end", f"{speaker}\n", "trinity")
        elif "📚" in speaker:
            self.chat_box.insert("end", f"{speaker}\n", "memory")
        else:
            self.chat_box.insert("end", f"{speaker}\n", "child")
        self.chat_box.insert("end", f"{text}\n\n", tag)
        self.chat_box.see("end")
        self.chat_box.config(state="disabled")

    def set_status(self, text, color="#f0c040"):
        self.status_label.config(text=text, fg=color)
        self.root.update()

    def trim_history(self):
        if len(self.conversation_history) > 10:
            self.conversation_history = self.conversation_history[-10:]

    def toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self.mute_btn.config(
                text="🔇  Μικρόφωνο OFF",
                bg="#442222", fg="#ff8888"
            )
        else:
            self.mute_btn.config(
                text="🎤  Μικρόφωνο ON",
                bg="#224422", fg="#88ff88"
            )

    def confirm_clear_memory(self):
        pin_window = tk.Toplevel(self.root)
        pin_window.title("Επαλήθευση PIN")
        pin_window.geometry("300x180")
        pin_window.configure(bg="#1a1a2e")
        pin_window.resizable(False, False)
        pin_window.grab_set()

        tk.Label(pin_window, text="🔒  Εισάγετε PIN",
                 font=("Segoe UI", 16, "bold"),
                 bg="#1a1a2e", fg="#c8c8ff").pack(pady=(20, 5))

        tk.Label(pin_window, text="Απαιτείται PIN διαχειριστή",
                 font=("Segoe UI", 11),
                 bg="#1a1a2e", fg="#6666aa").pack()

        pin_var = tk.StringVar()
        pin_entry = tk.Entry(
            pin_window, textvariable=pin_var,
            font=("Segoe UI", 18), show="●",
            width=8, justify="center",
            bg="#080818", fg="white",
            insertbackground="white",
            relief="flat"
        )
        pin_entry.pack(pady=15)
        pin_entry.focus()

        def check_pin(event=None):
            admin_pin = config.get("admin_pin", "1234")
            if pin_var.get() == admin_pin:
                pin_window.destroy()
                if messagebox.askyesno(
                    "Διαγραφή Μνήμης",
                    f"Να διαγραφεί η μνήμη για τον {self.current_name};\nΘα ξεχαστούν όλες οι προηγούμενες συνεδρίες.",
                    icon="warning"
                ):
                    clear_memory(self.current_name)
                    self.current_memory = load_memory(self.current_name)
                    self.root.after(0, self.add_message,
                                   "🗑️ Σύστημα",
                                   "Η μνήμη διαγράφηκε. Η επόμενη συνεδρία ξεκινά από μηδέν.",
                                   "memory")
            else:
                pin_entry.config(fg="#ff4444")
                pin_var.set("")
                pin_entry.config(fg="white")

        pin_entry.bind("<Return>", check_pin)

        tk.Button(
            pin_window, text="Επιβεβαίωση",
            font=("Segoe UI", 12),
            bg="#2d6ae0", fg="white",
            relief="flat", cursor="hand2",
            padx=20, pady=6,
            command=check_pin
        ).pack()

    # ----------------------------------------------------------
    # SESSION LOGIC
    # ----------------------------------------------------------
    def start_session(self, child_display, child_name, system_prompt, child_folder):
        self.current_child  = child_display
        self.current_name   = child_name
        self.current_prompt = system_prompt
        self.current_folder = child_folder
        self.current_memory = load_memory(child_name)
        self.conversation_history = []
        self.session_active = True
        self.muted = False

        self.build_session(child_display)
        threading.Thread(target=self.run_session, daemon=True).start()

    def run_session(self):
        available_books = get_available_books(self.current_folder)
        self.page_cache = {}
        memory_text     = memory_to_prompt(self.current_memory, self.current_child)

        tools_prompt = f"""

ΣΥΣΤΗΜΑ ΒΙΒΛΙΩΝ — ΥΠΟΧΡΕΩΤΙΚΟ:
Τα παρακάτω βιβλία είναι διαθέσιμα στο σύστημα:
{available_books}

ΚΑΝΟΝΑΣ: Όταν το παιδί αναφέρει οποιαδήποτε σελίδα από βιβλίο, ΥΠΟΧΡΕΩΤΙΚΑ στέλνεις:
READ_BOOK:keyword:start_page:end_page

ΔΕΝ επιτρέπεται να πεις ότι δεν έχεις πρόσβαση.
ΔΕΝ επιτρέπεται να ζητήσεις από το παιδί να σου διαβάσει τη σελίδα.
ΔΕΝ επιτρέπεται να γράψεις ΤΙΠΟΤΑ άλλο μαζί με το READ_BOOK command.

Παραδείγματα:
- Παιδί: "Ιστορία σελίδα 17" → εσύ: READ_BOOK:istoria:17:19
- Παιδί: "Γλώσσα σελίδα 5" → εσύ: READ_BOOK:glossa:5:7
{memory_text}"""

        full_prompt = self.current_prompt + tools_prompt

        if self.current_memory["last_session"]:
            mem_summary = f"Τελευταία συνεδρία: {self.current_memory['last_session']}"
            if self.current_memory["last_books"]:
                books_info = ", ".join([f"{b['book']} σ.{b['last_page']}"
                                       for b in self.current_memory["last_books"]])
                mem_summary += f"\nΒιβλία: {books_info}"
            if self.current_memory["difficult_topics"]:
                mem_summary += f"\nΔύσκολα θέματα: {', '.join(self.current_memory['difficult_topics'])}"
            self.root.after(0, self.add_message, "📚 Μνήμη", mem_summary, "memory")

        intro = (f"Γεια σου {self.current_child}! Χαίρομαι που σε βλέπω! "
                 f"Τι μαθήματα έχουμε σήμερα και ποιες σελίδες θα κάνουμε;")

        self.conversation_history.append({"role": "assistant", "content": intro})
        self.root.after(0, self.add_message, "✨ Trinity", intro)
        self.set_status("🔊  Trinity μιλά...", "#00ff88")
        if self.pulse:
            self.pulse.set_mode("speaking")
        text_to_speech(intro)
        if self.pulse:
            self.pulse.set_mode("idle")

        while self.session_active:
            # Έλεγξε αν είναι muted
            while self.muted and self.session_active:
                self.set_status("🔇  Μικρόφωνο σε σίγαση...", "#ff8888")
                if self.pulse:
                    self.pulse.set_mode("idle")
                time.sleep(0.2)

            if not self.session_active:
                break

            self.set_status("🎤  Σε ακούω...", "#00cc66")
            if self.pulse:
                self.pulse.set_mode("listening")
            audio_data = record_audio()

            if not self.session_active:
                break
            if audio_data is None:
                continue

            if self.pulse:
                self.pulse.set_mode("thinking")
            self.set_status("⏳  Επεξεργασία...", "#f0c040")

            try:
                audio_file = save_audio(audio_data)
                user_text  = speech_to_text(audio_file)
                os.unlink(audio_file)

                self.root.after(0, self.add_message,
                                f"👦 {self.current_child}", user_text)

                self.conversation_history.append({
                    "role": "user", "content": user_text
                })

                self.trim_history()
                self.set_status("🧠  Trinity σκέφτεται...", "#00ccaa")

                response = anthropic_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=700,
                    system=full_prompt,
                    messages=self.conversation_history
                )

                trinity_text = response.content[0].text.strip()

                if trinity_text.startswith("READ_BOOK:"):
                    parts = trinity_text.split(":")
                    if len(parts) >= 3:
                        keyword    = parts[1].strip()
                        start_page = int(parts[2].strip())
                        end_page   = int(parts[3].strip()) if len(parts) > 3 else start_page + 1

                        self.set_status("📖  Διαβάζω βιβλίο...", "#aaaaff")

                        cache_key = f"{keyword}:{start_page}:{end_page}"
                        if cache_key in self.page_cache:
                            book_content = self.page_cache[cache_key]
                        else:
                            book_content = read_book_page(
                                self.current_folder, keyword, start_page, end_page)
                            self.page_cache[cache_key] = book_content

                        self.conversation_history.append({
                            "role": "assistant", "content": trinity_text
                        })
                        self.conversation_history.append({
                            "role": "user",
                            "content": f"[ΠΕΡΙΕΧΟΜΕΝΟ ΒΙΒΛΙΟΥ - σελίδες {start_page}-{end_page}]:\n{book_content}"
                        })

                        self.set_status("🧠  Trinity σκέφτεται...", "#00ccaa")

                        response2 = anthropic_client.messages.create(
                            model="claude-sonnet-4-6",
                            max_tokens=700,
                            system=full_prompt,
                            messages=self.conversation_history
                        )
                        trinity_text = response2.content[0].text

                        self.conversation_history.pop()
                        self.conversation_history.pop()
                        self.conversation_history.append({
                            "role": "assistant", "content": trinity_text
                        })
                    else:
                        self.conversation_history.append({
                            "role": "assistant", "content": trinity_text
                        })
                else:
                    self.conversation_history.append({
                        "role": "assistant", "content": trinity_text
                    })

                self.root.after(0, self.add_message, "✨ Trinity", trinity_text)
                self.set_status("🔊  Trinity μιλά...", "#00ff88")
                if self.pulse:
                    self.pulse.set_mode("speaking")
                text_to_speech(trinity_text)
                if self.pulse:
                    self.pulse.set_mode("idle")

            except Exception as e:
                self.set_status(f"⚠️  Σφάλμα: {str(e)[:80]}", "#ef5350")
                if self.pulse:
                    self.pulse.set_mode("idle")

    def go_home(self):
        self.session_active = False
        if self.current_name and self.conversation_history:
            self.set_status("💾  Αποθηκεύω μνήμη...", "#aaaaff")
            updated_memory = extract_memory_update(
                self.current_name,
                self.conversation_history,
                self.current_memory
            )
            save_memory(self.current_name, updated_memory)
        self.build_home()

# ============================================================
# MAIN
# ============================================================
def main():
    root = tk.Tk()
    app = TrinityApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()