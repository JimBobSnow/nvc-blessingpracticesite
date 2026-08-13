#!/usr/bin/env python3
"""NVC + Blessing Practice Coach — standalone app backed by OpenRouter.

Three areas:
  - Practice Session: pick what/how to practice from real menus, then chat.
  - Learning Library: browseable reference covering every mode and skill
    in the framework (empathic listening, self-expression, blessing, and
    the 6-step guessing drill) — no API call needed.
  - Settings: OpenRouter API key + model slug.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from tkinter import messagebox, ttk

NVC_DOC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NVC & BLESSING.md")
CONFIG_DIR = os.path.expanduser("~/.config/nvc-practice-app")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CHATS_DIR = os.path.join(CONFIG_DIR, "chats")
LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def strip_emphasis_from_numbered_lines(text):
    # Deterministic backstop: prompt instructions alone don't reliably stop
    # weaker models from bolding/italicizing the answer inside a drill
    # practice sentence, which hands it to the user before they try. Models
    # format practice items inconsistently (numbered "1. ...", bulleted
    # "- **Sentence 1:** ...", etc.), so this matches any list-item line
    # rather than one specific shape.
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if LIST_ITEM_RE.match(line):
            line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
            line = re.sub(r"\*(.*?)\*", r"\1", line)
            line = re.sub(r"__(.*?)__", r"\1", line)
            line = re.sub(r"_(.*?)_", r"\1", line)
            lines[i] = line
    return "\n".join(lines)


DEFAULT_MODEL = "anthropic/claude-sonnet-5"

PROVIDERS = ["OpenRouter (API key)", "Claude Code (local CLI, subscription)"]
CLAUDE_CLI_MODELS = ["haiku", "sonnet", "opus"]
CLAUDE_CLI_EFFORTS = ["low", "medium", "high", "max"]
DEFAULT_CLAUDE_CLI_MODEL = "haiku"
DEFAULT_CLAUDE_CLI_EFFORT = "low"

BG = "#161616"
PANEL_BG = "#1e1e1e"
FG = "#e6e6e6"
ACCENT = "#8ab4f8"
MUTED = "#8a8a8a"
INPUT_BG = "#232323"
SELECT_BG = "#3a4a63"

SIDEBAR_BG = "#0e0e0e"
SIDEBAR_HOVER = "#1c1c1c"
SIDEBAR_SELECT = "#242424"
SIDEBAR_BORDER = "#262626"
BUBBLE_BG = "#2a2a2a"
SEND_BTN_BG = "#4a7ddb"


# ---------------------------------------------------------------------------
# Text-editing quality-of-life helpers, applied to every Entry/Text field
# below: Ctrl+A select-all, a right-click Cut/Copy/Paste/Select All menu,
# and undo/redo for the multi-line chat box.
# ---------------------------------------------------------------------------

def _select_all(widget):
    if isinstance(widget, tk.Entry):
        widget.selection_range(0, "end")
        widget.icursor("end")
    else:
        widget.tag_add("sel", "1.0", "end")
    return "break"


def add_select_all(widget):
    widget.bind("<Control-a>", lambda e: _select_all(widget))
    widget.bind("<Control-A>", lambda e: _select_all(widget))


def _safe_generate(widget, virtual_event):
    try:
        widget.event_generate(virtual_event)
    except tk.TclError:
        pass


def _paste_replacing_selection(widget):
    # Tk's built-in <<Paste>> on X11/Linux inserts at the cursor without
    # deleting the current selection first — this overrides that so paste
    # always replaces a selection, matching every other platform's behavior.
    try:
        clipboard = widget.clipboard_get()
    except tk.TclError:
        return "break"
    try:
        widget.delete("sel.first", "sel.last")
    except tk.TclError:
        pass
    widget.insert("insert", clipboard)
    if isinstance(widget, tk.Text):
        widget.see("insert")
    return "break"


def add_paste_override(widget):
    widget.bind("<Control-v>", lambda e: _paste_replacing_selection(widget))
    widget.bind("<Control-V>", lambda e: _paste_replacing_selection(widget))


def add_context_menu(widget, editable=True):
    menu = tk.Menu(
        widget, tearoff=0, bg=INPUT_BG, fg=FG,
        activebackground=SELECT_BG, activeforeground="#ffffff",
    )
    if editable:
        menu.add_command(label="Cut", command=lambda: _safe_generate(widget, "<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: _safe_generate(widget, "<<Copy>>"))
    if editable:
        menu.add_command(label="Paste", command=lambda: _paste_replacing_selection(widget))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: _select_all(widget))

    def show_menu(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind("<Button-3>", show_menu)
    return menu


def add_undo_redo(widget):
    def undo(event):
        try:
            widget.edit_undo()
        except tk.TclError:
            pass
        return "break"

    def redo(event):
        try:
            widget.edit_redo()
        except tk.TclError:
            pass
        return "break"

    widget.bind("<Control-z>", undo)
    widget.bind("<Control-Z>", redo)
    widget.bind("<Control-y>", redo)


PRACTICE_TYPES = [
    "Empathic listening",
    "NVC self-expression",
    "Blessing",
    "NVC + blessing together",
    "Real-life situation",
    "Step-by-step guessing drill",
]

DIFFICULTIES = ["Beginner", "Intermediate", "Advanced"]

DRILL_LEVELS = [
    "Level 1 — Charged word or phrase",
    "Level 2 — Add Function",
    "Level 3 — Add Feeling family",
    "Level 4 — Add Need direction",
    "Level 5 — Add Permission to not know",
    "Level 6 — Add Check against their words",
    "Capstone — all six steps together",
]

# ---------------------------------------------------------------------------
# Learning Library content
#
# Each node: {"id", "label", "content": [blocks], "children": [nodes]}
# Block types:
#   ("para", text)
#   ("subheading", text)
#   ("bullets", [items])
#   ("example", quote, note)
# ---------------------------------------------------------------------------

LEARN_TREE = [
    {
        "id": "overview",
        "label": "Overview",
        "content": [
            ("para",
             "This app teaches two complementary skills: Nonviolent "
             "Communication (NVC) — both empathic listening and honest "
             "self-expression — and the practice of blessing others, "
             "using a four-step framework."),
            ("para",
             "NVC has two directions. Empathic listening turns your "
             "attention toward another person's experience with "
             "curiosity and compassion. Self-expression means "
             "communicating your own observations, feelings, needs, and "
             "requests honestly, without blame or judgment."),
            ("para",
             "Blessing is a separate but complementary skill: helping "
             "someone feel secure in your love, helping them see a "
             "genuine virtue in themselves, affirming that virtue as "
             "part of their identity, and connecting that identity to a "
             "positive future — without denying present pain."),
            ("para",
             "The goal is not memorizing formulas. It's becoming someone "
             "who naturally listens deeply, stays curious, communicates "
             "honestly, and helps people feel secure, seen, and hopeful."),
            ("subheading", "Core principle"),
            ("bullets", [
                "Connection before correction.",
                "Understanding before advice.",
                "Love before identity.",
                "Identity before possibility.",
            ]),
        ],
    },
    {
        "id": "modes",
        "label": "Practice Modes (what each menu option does)",
        "content": [
            ("para",
             "These match the \"Practice type\" dropdown on the Practice "
             "Session tab. Pick a topic on the left to see what actually "
             "happens in that mode."),
        ],
        "children": [
            {
                "id": "modes.empathic",
                "label": "Empathic listening",
                "content": [
                    ("para",
                     "Your job is to help them respond with "
                     "empathy — not fix, advise, defend, correct, or "
                     "cheerlead. You try first; the coach won't hand you "
                     "the \"correct\" feeling or need in advance."),
                    ("para",
                     "After you respond, the coach explains how well the "
                     "guess connected and what alternative feelings/needs "
                     "were also plausible."),
                ],
            },
            {
                "id": "modes.expression",
                "label": "NVC self-expression",
                "content": [
                    ("para",
                     "The coach gives you a conflict scenario. You "
                     "practice building the four components yourself: "
                     "Observation, Feeling, Need, Request."),
                    ("para", "The coach then evaluates specifically:"),
                    ("bullets", [
                        "Did the observation contain judgments?",
                        "Were the feelings real, or thoughts disguised as feelings?",
                        "Were the needs distinct from strategies?",
                        "Was the request concrete and doable — or secretly a demand?",
                        "Did the overall message preserve connection?",
                    ]),
                ],
            },
            {
                "id": "modes.blessing",
                "label": "Blessing",
                "content": [
                    ("para",
                     "The coach gives a scenario involving someone who "
                     "has failed, been hurt, or is stuck in a painful "
                     "identity. You practice the four-step blessing "
                     "framework: security, virtue, identity, positive "
                     "outcome."),
                    ("para",
                     "You attempt it yourself first — the coach won't "
                     "just hand you the finished blessing."),
                ],
            },
            {
                "id": "modes.combined",
                "label": "NVC + blessing together",
                "content": [
                    ("para",
                     "Combined scenarios where you practice empathy "
                     "first — connecting with what's alive in the other "
                     "person — and only move into blessing afterward. "
                     "Blessing is never used to rush past or deny real "
                     "pain."),
                ],
            },
            {
                "id": "modes.reallife",
                "label": "Real-life situation",
                "content": [
                    ("para",
                     "You bring an actual situation from your life. The "
                     "coach won't immediately write the perfect response "
                     "for you. It walks you through discovery questions "
                     "first, on both the NVC side and the blessing side, "
                     "and only helps refine your own words afterward."),
                    ("subheading", "NVC discovery questions"),
                    ("bullets", [
                        "What happened?",
                        "What did you actually observe?",
                        "What are you feeling?",
                        "What need is underneath it?",
                        "What would you like to request?",
                        "What might the other person be feeling?",
                        "What might they be needing?",
                    ]),
                    ("subheading", "Blessing discovery questions"),
                    ("bullets", [
                        "What happened to this person?",
                        "What might they currently be attached to or defined by?",
                        "How can you make them feel secure in your love?",
                        "What virtues do you genuinely see?",
                        "What virtue might they have difficulty seeing?",
                        "What does that reveal about their identity?",
                        "What positive outcomes could flow from that identity?",
                    ]),
                ],
            },
            {
                "id": "modes.drill",
                "label": "Step-by-step guessing drill",
                "content": [
                    ("para",
                     "A progressive way to learn how to decode a sentence "
                     "into a feeling and a need, one small skill at a "
                     "time instead of all six at once."),
                    ("para",
                     "Pick a starting Level (1–6) in the \"Drill level\" "
                     "dropdown — each level explains the step, shows "
                     "worked examples, then gives you a small round of "
                     "practice sentences with coaching. You decide when "
                     "you're ready to move to the next level; nothing "
                     "advances automatically. Once all six have been "
                     "practiced individually, choose \"Capstone\" to run "
                     "the full six-step sequence unscaffolded on a new "
                     "sentence."),
                    ("para",
                     "See the \"6-Step Guessing Drill\" section for what "
                     "each step actually means, with examples."),
                ],
            },
        ],
    },
    {
        "id": "listening",
        "label": "Empathic Listening Skills",
        "content": [
            ("para",
             "The underlying skills used in \"Empathic listening\" mode, "
             "and in any moment where someone needs to feel heard."),
        ],
        "children": [
            {
                "id": "listening.goal",
                "label": "The Goal",
                "content": [
                    ("para",
                     "When listening empathically, the goal is NOT to "
                     "fix, give advice, defend yourself, correct their "
                     "interpretation, explain why they're wrong, "
                     "cheerlead, minimize their pain, immediately tell "
                     "them what to do, turn the conversation toward "
                     "yourself, interrogate, or diagnose."),
                    ("para",
                     "The goal is to connect with what is alive in the "
                     "other person. Listen for: what happened, what are "
                     "they feeling, what might they be needing."),
                    ("para",
                     "You don't have to agree with someone's "
                     "interpretation in order to empathize with the "
                     "feelings and needs underneath it."),
                ],
            },
            {
                "id": "listening.guessing",
                "label": "Empathic Guessing",
                "content": [
                    ("para",
                     "NVC empathy often involves making tentative "
                     "guesses, not confident diagnoses."),
                    ("example",
                     "\"My boss completely humiliated me in front of everyone.\"",
                     "instead of \"Your boss is awful,\" try \"Are you "
                     "feeling embarrassed and hurt, and really wanting "
                     "respect?\""),
                    ("para", "Useful tentative phrasings:"),
                    ("bullets", [
                        "\"Are you feeling...?\"",
                        "\"I'm wondering if...\"",
                        "\"Could it be that you're feeling...?\"",
                        "\"Is there some...\"",
                        "\"Are you needing...?\"",
                    ]),
                    ("para",
                     "If the person says \"No, that's not it,\" get "
                     "curious rather than defensive: \"Okay. What is "
                     "it?\" Empathy is not guessing correctly — it's "
                     "staying curious enough to keep discovering the "
                     "person's experience."),
                ],
            },
            {
                "id": "listening.reflective",
                "label": "Reflective Listening",
                "content": [
                    ("para",
                     "Reflect what you hear without parroting every "
                     "word. A useful reflection can contain a feeling "
                     "(\"It sounds like you're really disappointed\"), a "
                     "need (\"and maybe you really wanted to feel valued "
                     "and included\"), or the meaning/experience "
                     "(\"you had been looking forward to this, so being "
                     "left out really hurt\")."),
                    ("para",
                     "Don't force every response into the rigid formula "
                     "\"You're feeling X because you need Y.\" Natural "
                     "human connection matters more than the formula."),
                ],
            },
            {
                "id": "listening.agreeing",
                "label": "Listening Without Agreeing",
                "content": [
                    ("para", "Empathy is not the same as agreement."),
                    ("example",
                     "\"You're furious because you felt betrayed.\"",
                     "you can understand this without also saying "
                     "\"Yes, they absolutely betrayed you.\" You can "
                     "understand someone's experience without agreeing "
                     "with their conclusions, accusations, judgments, or "
                     "proposed actions — especially important during "
                     "conflict."),
                ],
            },
            {
                "id": "listening.judgments",
                "label": "Needs Under Judgments",
                "content": [
                    ("para",
                     "Translate judgments into possible feelings and "
                     "needs. This isn't about proving the judgment "
                     "false — it's about discovering the human "
                     "experience underneath it."),
                    ("example", "\"You don't care about me.\"",
                     "possibly hurt, lonely, and needing connection and "
                     "reassurance."),
                    ("example", "\"He's such a selfish person.\"",
                     "possibly frustrated and needing consideration and "
                     "fairness."),
                    ("example", "\"Nobody respects me.\"",
                     "possibly discouraged and needing dignity, "
                     "appreciation, or recognition."),
                ],
            },
            {
                "id": "listening.anger",
                "label": "When Someone Is Angry",
                "content": [
                    ("para",
                     "Don't rush to calm or correct an angry person — "
                     "get curious about the anger instead."),
                    ("bullets", [
                        "\"Are you really angry because something important to you wasn't respected?\"",
                        "\"Are you feeling hurt underneath the anger?\"",
                        "\"Did you really want to be heard?\"",
                        "\"Are you needing fairness here?\"",
                    ]),
                    ("para",
                     "Anger can contain valuable information about "
                     "needs, values, boundaries, or violated "
                     "expectations."),
                ],
            },
            {
                "id": "listening.fixing",
                "label": "Empathy Without Fixing",
                "content": [
                    ("example", "\"I'm terrified I'm going to fail.\"",
                     "don't jump to \"You'll do great!\" First connect: "
                     "\"Are you really scared because this matters so "
                     "much to you?\" Only once the person feels "
                     "understood does encouragement become appropriate."),
                    ("para", "The sequence: connect first, solve later."),
                ],
            },
        ],
    },
    {
        "id": "expression",
        "label": "Self-Expression Skills",
        "content": [
            ("para",
             "The other side of NVC: honestly communicating what "
             "happened, what you feel, what you need, and what you'd "
             "like — without blame, criticism, diagnosis, demands, or "
             "coercion. Four components: Observation, Feeling, Need, "
             "Request."),
        ],
        "children": [
            {
                "id": "expression.observation",
                "label": "Observation vs Judgment",
                "content": [
                    ("para",
                     "Identify what actually happened without adding "
                     "judgment, interpretation, diagnosis, exaggeration, "
                     "or accusation. Useful test: \"What could a camera "
                     "or microphone have recorded?\""),
                    ("example", "\"You were completely disrespectful.\"",
                     "observation instead: \"When I was talking, you "
                     "interrupted me three times and then left the "
                     "room.\""),
                    ("example", "\"You never listen to me.\"",
                     "observation instead: \"When I was telling you "
                     "about my day, you looked at your phone and didn't "
                     "respond.\""),
                    ("example", "\"You're always late.\"",
                     "observation instead: \"We agreed to meet at 6:00, "
                     "and you arrived at 6:30.\""),
                    ("para",
                     "Observation = what actually happened. "
                     "Interpretation = what I think the behavior means. "
                     "Judgment = my evaluation of the behavior or "
                     "person. NVC doesn't require you to deny your "
                     "interpretation or judgment internally — it asks "
                     "you to communicate the concrete observation first "
                     "so the conversation has shared factual ground."),
                ],
            },
            {
                "id": "expression.feeling",
                "label": "Feeling",
                "content": [
                    ("para", "Possible feelings include:"),
                    ("bullets", [
                        "sad, hurt, afraid, frustrated, disappointed",
                        "lonely, confused, overwhelmed, relieved, grateful",
                        "joyful, hopeful, anxious, angry, embarrassed",
                        "discouraged, worried, helpless, excited, tender",
                    ]),
                    ("para",
                     "Distinguish genuine feelings from thoughts, "
                     "accusations, and interpretations disguised as "
                     "feelings, e.g. \"I feel ignored,\" \"I feel "
                     "manipulated,\" \"I feel betrayed,\" \"I feel like "
                     "you don't care.\" These carry information, but "
                     "they're primarily interpretations about someone "
                     "else's behavior, not straightforward descriptions "
                     "of your own emotional experience."),
                    ("example", "\"I feel ignored.\"",
                     "try instead: \"I feel hurt and lonely.\""),
                    ("example", "\"I feel disrespected.\"",
                     "try instead: \"I feel angry and disappointed.\""),
                    ("example", "\"I feel like you don't care about me.\"",
                     "try instead: \"I feel sad and disconnected.\""),
                    ("para",
                     "There isn't only one correct feeling — people can "
                     "feel several things at once."),
                ],
            },
            {
                "id": "expression.need",
                "label": "Need vs Strategy",
                "content": [
                    ("para", "Needs can include:"),
                    ("bullets", [
                        "connection, respect, honesty, safety, belonging",
                        "consideration, autonomy, understanding, appreciation, trust",
                        "support, reliability, rest, play, meaning",
                        "contribution, fairness, companionship, acceptance, freedom",
                        "recognition, cooperation, intimacy, choice, dignity, reassurance",
                    ]),
                    ("para",
                     "A need is universal and human. A strategy is one "
                     "particular way of trying to meet that need — there "
                     "may be many possible strategies for the same "
                     "need."),
                    ("example", "\"I need you to call me every night.\"",
                     "strategy — underlying needs might be connection, "
                     "reassurance, and reliability."),
                    ("example", "\"I need you to apologize.\"",
                     "strategy — underlying needs might be "
                     "acknowledgment, accountability, repair, and "
                     "respect."),
                    ("example", "\"I need you to stop talking to him.\"",
                     "strategy — underlying needs might be safety, "
                     "trust, reassurance, or consideration."),
                ],
            },
            {
                "id": "expression.request",
                "label": "Request vs Demand",
                "content": [
                    ("para", "A good request is generally:"),
                    ("bullets", [
                        "clear, concrete, specific",
                        "positive/actionable rather than just what should stop",
                        "realistic and directed at something the other person can actually do",
                        "open to hearing \"no\"",
                    ]),
                    ("example", "\"Please be more considerate.\"",
                     "stronger: \"Would you be willing to let me finish "
                     "speaking before responding?\""),
                    ("example", "\"I need you to communicate better.\"",
                     "stronger: \"Would you be willing to text me if "
                     "you're going to be more than 15 minutes late?\""),
                    ("example", "\"Stop ignoring me.\"",
                     "stronger: \"Would you be willing to tell me when "
                     "you're too overwhelmed to talk and suggest a time "
                     "we can reconnect?\""),
                    ("para",
                     "A request is not a demand — the other person is "
                     "genuinely allowed to say no. If you can't tolerate "
                     "a \"no,\" explore what's underneath that reaction. "
                     "The goal isn't to manipulate compliance; it's to "
                     "create the possibility of mutually caring about "
                     "everyone's needs."),
                ],
            },
            {
                "id": "expression.together",
                "label": "Putting It Together",
                "content": [
                    ("para",
                     "Combine the four components naturally rather than "
                     "mechanically. Example:"),
                    ("example",
                     "Observation: \"When I was talking about what "
                     "happened today, you looked at your phone and "
                     "didn't respond.\"",
                     "Feeling: \"I felt hurt and disconnected.\" Need: "
                     "\"I really value being heard and feeling connected "
                     "with you.\" Request: \"Would you be willing to put "
                     "your phone down and listen to me for a few "
                     "minutes?\""),
                    ("para",
                     "The final message doesn't have to announce "
                     "\"Observation. Feeling. Need. Request.\" The four "
                     "components are a framework for thinking and "
                     "connecting, not a script that has to sound "
                     "formulaic."),
                ],
            },
        ],
    },
    {
        "id": "blessing",
        "label": "Blessing Framework",
        "content": [
            ("para",
             "A four-step framework (based on Alan Wright's worksheet) "
             "for helping someone feel secure, seen, and hopeful."),
        ],
        "children": [
            {
                "id": "blessing.security",
                "label": "Step 1 — Security and Freedom",
                "content": [
                    ("para",
                     "Make sure the person feels safe and secure in your "
                     "love after what has happened. Help them understand "
                     "they are still loved, and that the failure, "
                     "disappointment, or painful experience does not "
                     "define them — they can break free from the "
                     "identity created by what happened."),
                    ("para",
                     "The goal is NOT \"forget what happened.\" The goal "
                     "is: \"What happened is real, but it does not have "
                     "to define who you are.\" Don't minimize the pain."),
                ],
            },
            {
                "id": "blessing.virtue",
                "label": "Step 2 — See Their Virtues",
                "content": [
                    ("para",
                     "Once the person is no longer beholden to what "
                     "happened, help them see who they truly are. Look "
                     "for genuine qualities such as:"),
                    ("bullets", [
                        "courage, compassion, perseverance, patience, wisdom",
                        "generosity, faithfulness, honesty, humility, creativity",
                        "resilience, kindness, loyalty, tenderness, leadership",
                        "thoughtfulness, determination",
                    ]),
                    ("para",
                     "Do not invent virtues merely to sound "
                     "encouraging — the virtue has to be genuinely "
                     "observed."),
                ],
            },
            {
                "id": "blessing.identity",
                "label": "Step 3 — Affirm the Virtue as Identity",
                "content": [
                    ("para",
                     "Reinforce the virtue as part of the person's "
                     "identity. Move from \"You showed courage\" toward "
                     "\"You are courageous.\" The goal is to help the "
                     "person recognize something true about who they "
                     "are — keep it grounded in real evidence, not "
                     "flattery."),
                ],
            },
            {
                "id": "blessing.outcome",
                "label": "Step 4 — Connect Identity to Positive Outcomes",
                "content": [
                    ("para",
                     "Attach the identity to positive possibilities in "
                     "life. Ask: \"Because this is who they are, what "
                     "good might come from that?\""),
                    ("example",
                     "Example",
                     "\"If you continue bringing that compassion into "
                     "your relationships, I believe people will "
                     "experience you as someone they can feel safe "
                     "with.\""),
                    ("para",
                     "The outcome should flow naturally from the "
                     "virtue. Don't make promises about specific future "
                     "events."),
                ],
            },
        ],
    },
    {
        "id": "combine",
        "label": "Combining NVC + Blessing",
        "content": [
            ("para",
             "NVC empathy asks: \"What is this person experiencing? "
             "What feelings and needs are alive here?\" Blessing then "
             "asks: \"How can I help this person feel secure in my "
             "love? What virtues do I see? How can I help them "
             "recognize those virtues as identity? What positive "
             "possibilities flow from that identity?\""),
            ("para",
             "A powerful interaction may look like: Listen → Empathize "
             "→ Understand → Secure → See virtue → Affirm identity → "
             "Speak toward possibility."),
            ("para",
             "But this sequence should never be forced onto every "
             "conversation. Sometimes a person needs empathy and "
             "nothing more. Sometimes they need a boundary. Sometimes "
             "they need a request. Sometimes a blessing would be "
             "premature or inappropriate. Part of the skill is "
             "discerning what the moment actually calls for."),
        ],
    },
    {
        "id": "drill",
        "label": "6-Step Guessing Drill",
        "content": [
            ("para",
             "The six-step process for decoding what's underneath a "
             "sentence — and the basis of the \"Step-by-step guessing "
             "drill\" practice mode, which teaches these one level at a "
             "time (see Practice Modes) before combining them all in a "
             "Capstone round."),
        ],
        "children": [
            {
                "id": "drill.1",
                "label": "1. Charged word or phrase",
                "content": [
                    ("para",
                     "Find the specific word or phrase doing the "
                     "emotional work — not the whole sentence. Look for "
                     "absolutes (\"never\", \"always\", \"not even\"), "
                     "self-labels (\"I'm such an idiot\"), or "
                     "repeated/exaggerated phrases. That's where the "
                     "charge lives — everything else in the sentence is "
                     "usually just context."),
                    ("example", "\"I'm not even allowed to be this upset.\"",
                     "\"not even allowed\" — a self-imposed rule, not a "
                     "description of what happened."),
                    ("example", "\"You never listen to me.\"",
                     "\"never\" — an absolute. One frustrating moment "
                     "becomes a permanent verdict."),
                    ("example", "\"Whatever, I don't even care anymore.\"",
                     "the flatness of \"whatever\" plus \"even\" — the "
                     "flatness itself is the signal, not the literal "
                     "claim of not caring."),
                    ("example", "\"I completely screwed up that presentation.\"",
                     "\"completely\" — turns one mistake into a total "
                     "failure."),
                    ("example", "\"Nobody ever checks in on me anymore.\"",
                     "\"nobody ever\" — a double absolute (person + "
                     "frequency)."),
                ],
            },
            {
                "id": "drill.2",
                "label": "2. Function",
                "content": [
                    ("para",
                     "Ask what the statement is DOING, not just what it "
                     "says: blaming someone else, blaming/policing "
                     "themselves, minimizing, justifying, or protecting. "
                     "Self-policing language usually sits on top of "
                     "shame, discouragement, or fear of being \"too "
                     "much\"."),
                    ("example", "\"I'm not even allowed to be this upset.\"",
                     "self-policing — attacking their own right to feel "
                     "what they feel."),
                    ("example", "\"Whatever, I don't even care anymore.\"",
                     "protecting/minimizing — distancing from the "
                     "situation so it can't hurt as much."),
                    ("example", "\"He never listens to me.\"",
                     "other-blaming — the complaint is aimed outward."),
                    ("example", "\"I guess my opinion doesn't matter here.\"",
                     "resigned minimizing, with an implicit complaint "
                     "aimed at the people around them, not just at "
                     "themselves."),
                    ("example", "\"I can't believe I let this happen again.\"",
                     "self-blame — judging their own repeated behavior."),
                ],
            },
            {
                "id": "drill.3",
                "label": "3. Feeling family",
                "content": [
                    ("para",
                     "Sort into a broad family first, before hunting for "
                     "the exact word: mad / sad / scared / glad / "
                     "ashamed. Being roughly right in the right "
                     "neighborhood is enough to start — precision comes "
                     "from their reaction, not your first guess."),
                    ("example", "\"I'm not even allowed to be this upset.\"",
                     "ashamed — self-judgment, feeling wrong for having "
                     "the feeling at all."),
                    ("example", "\"Whatever, I don't even care anymore.\"",
                     "sad/mad, masked by flat affect — the flatness is "
                     "often a shield over hurt, not the absence of it."),
                    ("example", "\"What if I mess this up in front of everyone?\"",
                     "scared — even though no feeling word was used."),
                    ("example", "\"I did everything right and it still wasn't enough.\"",
                     "discouraged/sad, often with a thread of anger "
                     "underneath."),
                    ("example", "\"I can't believe I let this happen again.\"",
                     "embarrassed/ashamed family — self-directed shock."),
                ],
            },
            {
                "id": "drill.4",
                "label": "4. Need direction",
                "content": [
                    ("para",
                     "Judgments aimed at OTHERS usually point to an "
                     "external need (respect, fairness, consideration). "
                     "Judgments aimed at THE SELF usually point to a "
                     "need for self-acceptance (permission, compassion, "
                     "validation, belonging). Check who the judgment is "
                     "aimed at before guessing the need."),
                    ("example", "\"I'm not even allowed to be this upset.\" (self-aimed)",
                     "self-acceptance — likely permission or "
                     "self-compassion, not something another person "
                     "needs to provide."),
                    ("example", "\"You never listen to me.\" (other-aimed)",
                     "external — likely to be heard, considered, or "
                     "respected."),
                    ("example", "\"I did everything right and it still wasn't enough.\"",
                     "mixed — external (recognition from others) and "
                     "self-facing (validation that the effort "
                     "mattered)."),
                    ("example", "\"I guess my opinion doesn't matter here.\"",
                     "external — points at the room/environment, likely "
                     "a need to be heard or included, not "
                     "self-acceptance."),
                    ("example", "\"I can't believe I let this happen again.\"",
                     "self-facing — likely self-compassion or "
                     "permission to make mistakes without it defining "
                     "them."),
                ],
            },
            {
                "id": "drill.5",
                "label": "5. Permission to not know",
                "content": [
                    ("para",
                     "You don't have to be certain. Naming uncertainty "
                     "out loud often surfaces more than a confident "
                     "wrong guess — and it keeps you from forcing a "
                     "guess that isn't really there yet."),
                    ("example",
                     "\"I don't even know why I'm upset, I just can't "
                     "stand being around them anymore.\"",
                     "instead of inventing a specific feeling: \"I'm not "
                     "totally sure what's under that, but it sounds like "
                     "something real — can you say more?\""),
                    ("example",
                     "A flat, ambiguous statement with no clear feeling word.",
                     "it's fine to name the ambiguity itself: \"Something "
                     "about this feels like a lot, even if it's hard to "
                     "name.\""),
                    ("example", "\"Whatever, I don't even care anymore.\"",
                     "could be anger, sadness, or exhaustion — all "
                     "plausible; naming the uncertainty (\"I can't tell "
                     "if this is anger or just being worn down\") can be "
                     "more honest than guessing one."),
                ],
            },
            {
                "id": "drill.6",
                "label": "6. Check against their words",
                "content": [
                    ("para",
                     "Before offering the guess, ask: did they actually "
                     "say or imply this, or would I feel this if it "
                     "happened to me? If it's the second one, drop it. "
                     "Read their words, not your own projected reaction "
                     "to the situation."),
                    ("example",
                     "\"They gave the promotion to someone with less experience.\"",
                     "weak guess: \"Are you needing financial "
                     "security?\" — money was never mentioned; this is a "
                     "projection."),
                    ("example",
                     "\"I did everything right and it still wasn't enough.\"",
                     "stronger guess: \"Are you needing to feel seen for "
                     "the effort you put in?\" — ties directly to \"did "
                     "everything right,\" which they actually said."),
                    ("example", "\"I'm not even allowed to be this upset.\"",
                     "weak guess: \"Are you afraid of conflict?\" — "
                     "nothing about conflict was said. Stronger: \"Are "
                     "you needing permission to just feel this?\" — ties "
                     "to \"not allowed.\""),
                ],
            },
        ],
    },
    {
        "id": "safety",
        "label": "Safety & Boundaries",
        "content": [
            ("para",
             "Empathy does not mean tolerating harmful behavior. "
             "Blessing does not mean excusing wrongdoing."),
            ("para",
             "A person can say \"What you did hurt me\" while also "
             "saying \"I still see good in you.\" A person can "
             "understand another person's feelings without agreeing "
             "with their interpretation. A person can love someone "
             "while setting a firm boundary."),
            ("para",
             "In situations involving abuse, coercion, manipulation, or "
             "danger, safety and appropriate boundaries come first — "
             "before empathy technique, before blessing, before any of "
             "the frameworks in this app."),
        ],
    },
]


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.chmod(CONFIG_PATH, 0o600)


def list_chats():
    if not os.path.isdir(CHATS_DIR):
        return []
    chats = []
    for fname in os.listdir(CHATS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(CHATS_DIR, fname), "r", encoding="utf-8") as f:
                chats.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    chats.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return chats


def save_chat(chat):
    os.makedirs(CHATS_DIR, exist_ok=True)
    path = os.path.join(CHATS_DIR, f"{chat['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(chat, f)
    os.chmod(path, 0o600)


def delete_chat(chat_id):
    path = os.path.join(CHATS_DIR, f"{chat_id}.json")
    if os.path.exists(path):
        os.remove(path)


def load_system_prompt():
    try:
        with open(NVC_DOC_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def call_openrouter(api_key, model, messages):
    body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def claude_cli_available():
    return shutil.which("claude") is not None


def call_claude_cli(prompt, model, effort, system_prompt=None, session_id=None):
    """Run the `claude` CLI in headless print mode as a subprocess.

    First call in a conversation passes --system-prompt and gets back a
    session_id; subsequent calls pass --resume <session_id> instead, so the
    CLI's own session tracks conversation history rather than this app
    resending it. No API key involved — auth is whatever `claude login`
    already set up (a Pro/Max/Team/Enterprise subscription).
    """
    cmd = [
        "claude", "--safe-mode", "-p", prompt,
        "--output-format", "json", "--tools", "",
        "--model", model, "--effort", effort,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    elif system_prompt:
        cmd += ["--system-prompt", system_prompt]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {result.returncode}: {result.stderr.strip() or '(no stderr)'}"
        )
    data = json.loads(result.stdout)
    if data.get("is_error"):
        raise RuntimeError(data.get("result", "Unknown error from claude CLI"))
    return data


class App(tk.Tk):
    NAV_ITEMS = [
        ("practice", "Practice Session"),
        ("learn", "Learning Library"),
        ("settings", "Settings"),
    ]

    def __init__(self):
        super().__init__()
        self.title("NVC + Blessing Practice Coach")
        self.geometry("1040x720")
        self.minsize(760, 520)
        self.resizable(True, True)
        self.configure(bg=BG)

        self.config_data = load_config()
        self.system_prompt = load_system_prompt()
        self.messages = []
        self.learn_nodes = {}
        self.current_practice_type = None
        self.current_chat_id = None
        self.current_difficulty = None
        self.current_drill_level = None
        self.claude_session_id = None
        self.pages = {}
        self.nav_buttons = {}
        self.active_page = "practice"

        self._setup_styles()

        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True)

        self._build_sidebar(root)

        right = tk.Frame(root, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_topbar(right)

        content = tk.Frame(right, bg=BG)
        content.pack(fill="both", expand=True)
        self.content_area = content

        self._build_practice_tab(content)
        self._build_learn_tab(content)
        self._build_settings_tab(content)

        self._show_page("practice")

    # ---------------- Sidebar + top bar ----------------

    def _build_sidebar(self, parent):
        sidebar = tk.Frame(parent, bg=SIDEBAR_BG, width=250)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        header = tk.Frame(sidebar, bg=SIDEBAR_BG)
        header.pack(fill="x", padx=18, pady=(20, 16))
        tk.Label(
            header, text="NVC + Blessing", bg=SIDEBAR_BG, fg=FG,
            font=("Sans", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header, text="Practice Coach", bg=SIDEBAR_BG, fg=MUTED,
            font=("Sans", 9),
        ).pack(anchor="w")

        nav_section = tk.Frame(sidebar, bg=SIDEBAR_BG)
        nav_section.pack(fill="x", padx=8)
        for key, label in self.NAV_ITEMS:
            self._make_nav_item(nav_section, key, label)

        tk.Frame(sidebar, bg=SIDEBAR_BORDER, height=1).pack(fill="x", padx=16, pady=12)

        tk.Label(
            sidebar, text="QUICK START", bg=SIDEBAR_BG, fg=MUTED,
            font=("Sans", 8, "bold"),
        ).pack(anchor="w", padx=18, pady=(0, 6))

        quick_frame = tk.Frame(sidebar, bg=SIDEBAR_BG)
        quick_frame.pack(fill="x", padx=8)
        for ptype in PRACTICE_TYPES:
            self._make_quick_item(quick_frame, ptype)

        tk.Frame(sidebar, bg=SIDEBAR_BORDER, height=1).pack(fill="x", padx=16, pady=12)

        chats_header = tk.Frame(sidebar, bg=SIDEBAR_BG)
        chats_header.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(
            chats_header, text="CHATS", bg=SIDEBAR_BG, fg=MUTED,
            font=("Sans", 8, "bold"),
        ).pack(side="left")
        new_chat_btn = tk.Label(
            chats_header, text="+ New", bg=SIDEBAR_BG, fg=ACCENT,
            font=("Sans", 8, "bold"), cursor="hand2",
        )
        new_chat_btn.pack(side="right")
        new_chat_btn.bind("<Button-1>", lambda e: self._start_new_chat())

        self.chats_frame = tk.Frame(sidebar, bg=SIDEBAR_BG)
        self.chats_frame.pack(fill="x", padx=8)
        self._refresh_chats_sidebar()

        tk.Frame(sidebar, bg=SIDEBAR_BG).pack(fill="both", expand=True)

        tk.Frame(sidebar, bg=SIDEBAR_BORDER, height=1).pack(fill="x", side="bottom")
        footer = tk.Frame(sidebar, bg=SIDEBAR_BG)
        footer.pack(fill="x", padx=18, pady=14, side="bottom")
        self.footer_label = tk.Label(
            footer, text=self._resolve_provider(), bg=SIDEBAR_BG, fg=MUTED,
            font=("Sans", 9),
        )
        self.footer_label.pack(anchor="w")

    def _make_nav_item(self, parent, key, label):
        btn = tk.Label(
            parent, text=label, bg=SIDEBAR_BG, fg=FG, font=("Sans", 10),
            anchor="w", padx=10, pady=8, cursor="hand2",
        )
        btn.pack(fill="x", pady=1)
        btn.bind("<Button-1>", lambda e, k=key: self._show_page(k))
        btn.bind("<Enter>", lambda e, k=key: self._on_nav_hover(k, True))
        btn.bind("<Leave>", lambda e, k=key: self._on_nav_hover(k, False))
        self.nav_buttons[key] = btn

    def _on_nav_hover(self, key, entering):
        btn = self.nav_buttons[key]
        if key == self.active_page:
            return
        btn.config(bg=SIDEBAR_HOVER if entering else SIDEBAR_BG)

    def _make_quick_item(self, parent, ptype):
        row = tk.Label(
            parent, text=ptype, bg=SIDEBAR_BG, fg=MUTED, font=("Sans", 9),
            anchor="w", padx=10, pady=5, cursor="hand2",
            wraplength=210, justify="left",
        )
        row.pack(fill="x")

        def select_type(event=None, ptype=ptype):
            self.practice_type.set(ptype)
            self._on_type_change(None)
            self._show_page("practice")

        row.bind("<Button-1>", select_type)
        row.bind("<Enter>", lambda e: row.config(bg=SIDEBAR_HOVER, fg=FG))
        row.bind("<Leave>", lambda e: row.config(bg=SIDEBAR_BG, fg=MUTED))

    def _refresh_chats_sidebar(self):
        for child in self.chats_frame.winfo_children():
            child.destroy()
        chats = list_chats()[:8]
        if not chats:
            tk.Label(
                self.chats_frame, text="No chats yet", bg=SIDEBAR_BG, fg=MUTED,
                font=("Sans", 8, "italic"), anchor="w", padx=10, pady=4,
            ).pack(fill="x")
            return
        for chat in chats:
            self._make_chat_item(self.chats_frame, chat)

    def _make_chat_item(self, parent, chat):
        is_active = chat["id"] == self.current_chat_id
        row_bg = SIDEBAR_SELECT if is_active else SIDEBAR_BG

        row = tk.Frame(parent, bg=row_bg)
        row.pack(fill="x")

        title_label = tk.Label(
            row, text=chat.get("title", "Untitled chat"),
            bg=row_bg, fg=ACCENT if is_active else MUTED,
            font=("Sans", 9), anchor="w", padx=10, pady=5, cursor="hand2",
            wraplength=170, justify="left",
        )
        title_label.pack(side="left", fill="x", expand=True)

        delete_label = tk.Label(
            row, text="×", bg=row_bg, fg=MUTED, font=("Sans", 11),
            cursor="hand2", padx=6,
        )
        delete_label.pack(side="right")

        def load(event=None, chat_id=chat["id"]):
            fresh = next((c for c in list_chats() if c["id"] == chat_id), None)
            if fresh:
                self._load_chat(fresh)

        def delete(event=None, chat_id=chat["id"], title=chat.get("title", "this chat")):
            if messagebox.askyesno("Delete chat", f'Delete "{title}"? This cannot be undone.'):
                delete_chat(chat_id)
                if chat_id == self.current_chat_id:
                    self.new_session()
                else:
                    self._refresh_chats_sidebar()

        title_label.bind("<Button-1>", load)
        delete_label.bind("<Button-1>", delete)

        if not is_active:
            def hover_on(e):
                row.config(bg=SIDEBAR_HOVER)
                title_label.config(bg=SIDEBAR_HOVER, fg=FG)
                delete_label.config(bg=SIDEBAR_HOVER)

            def hover_off(e):
                row.config(bg=SIDEBAR_BG)
                title_label.config(bg=SIDEBAR_BG, fg=MUTED)
                delete_label.config(bg=SIDEBAR_BG)

            title_label.bind("<Enter>", hover_on)
            title_label.bind("<Leave>", hover_off)

        delete_label.bind("<Enter>", lambda e: delete_label.config(fg="#e78585"))
        delete_label.bind("<Leave>", lambda e: delete_label.config(fg=MUTED))

    def _start_new_chat(self):
        self.new_session()
        self._show_page("practice")

    def _load_chat(self, chat):
        self.new_session()
        self.current_chat_id = chat["id"]
        self.current_practice_type = chat.get("practice_type")
        self.current_difficulty = chat.get("difficulty")
        self.current_drill_level = chat.get("drill_level")
        self.claude_session_id = chat.get("claude_session_id")
        self.messages = list(chat.get("messages", []))

        self._append("system", f"Resumed: {self.current_practice_type} ({self.current_difficulty})")
        for i, msg in enumerate(self.messages):
            if i == 0 and msg["role"] == "user":
                continue  # the internal [Practice setup] message, not shown live either
            tag = "user" if msg["role"] == "user" else "coach"
            self._append(tag, msg["content"])

        self._show_page("practice")
        self._refresh_chats_sidebar()

    def _build_topbar(self, parent):
        bar = tk.Frame(parent, bg=BG, height=54)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        inner = tk.Frame(bar, bg=BG)
        inner.pack(fill="both", expand=True, padx=22)

        self.topbar_title = tk.Label(
            inner, text="Practice Session", bg=BG, fg=FG, font=("Sans", 13, "bold"),
        )
        self.topbar_title.pack(side="left", pady=14)

        badge = tk.Label(
            inner, text="AI", bg=INPUT_BG, fg=MUTED, font=("Sans", 8, "bold"),
            padx=7, pady=2,
        )
        badge.pack(side="left", padx=(8, 0))

        tk.Frame(parent, bg=SIDEBAR_BORDER, height=1).pack(fill="x")

    def _show_page(self, key):
        self.active_page = key
        for k, frame in self.pages.items():
            frame.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for k, btn in self.nav_buttons.items():
            selected = k == key
            btn.config(
                bg=SIDEBAR_SELECT if selected else SIDEBAR_BG,
                fg=ACCENT if selected else FG,
            )
        titles = dict(self.NAV_ITEMS)
        self.topbar_title.config(text=titles[key])

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Dark.TLabel", background=BG, foreground=FG)
        style.configure(
            "Section.TLabel", background=BG, foreground=ACCENT,
            font=("Sans", 10, "bold"),
        )
        style.configure(
            "Title.TLabel", background=BG, foreground=ACCENT,
            font=("Sans", 13, "bold"),
        )
        style.configure(
            "Muted.TLabel", background=BG, foreground=MUTED,
            font=("Sans", 9),
        )
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab", background="#2a2a2a", foreground=FG,
            padding=(12, 7), font=("Sans", 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", BG)],
            foreground=[("selected", ACCENT)],
        )
        style.configure("TCombobox", fieldbackground=INPUT_BG, background=INPUT_BG)
        style.configure(
            "Dark.TLabelframe", background=BG, bordercolor=MUTED,
        )
        style.configure(
            "Dark.TLabelframe.Label", background=BG, foreground=ACCENT,
            font=("Sans", 10, "bold"),
        )
        style.configure(
            "Treeview", background=PANEL_BG, fieldbackground=PANEL_BG,
            foreground=FG, borderwidth=0, font=("Sans", 10),
        )
        style.map(
            "Treeview",
            background=[("selected", SELECT_BG)],
            foreground=[("selected", "#ffffff")],
        )

    # ---------------- Practice Session tab ----------------

    def _build_practice_tab(self, parent):
        page = ttk.Frame(parent, style="Dark.TFrame")
        self.pages["practice"] = page

        setup_outer = tk.Frame(
            page, bg=BG, highlightthickness=1,
            highlightbackground=SIDEBAR_BORDER, highlightcolor=SIDEBAR_BORDER,
        )
        setup_outer.pack(fill="x", padx=10, pady=10)

        self.setup_collapsed = False
        self.setup_header_label = tk.Label(
            setup_outer, text="▾  Session Setup", bg=BG, fg=ACCENT,
            font=("Sans", 10, "bold"), anchor="w", padx=10, pady=8, cursor="hand2",
        )
        self.setup_header_label.pack(fill="x")
        self.setup_header_label.bind("<Button-1>", lambda e: self._toggle_setup_panel())

        setup = tk.Frame(setup_outer, bg=BG)
        setup.pack(fill="x", padx=4, pady=(0, 6))
        self.setup_body = setup

        ttk.Label(setup, text="1. What do you want to practice?", style="Dark.TLabel").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 3)
        )
        self.practice_type = ttk.Combobox(
            setup, values=PRACTICE_TYPES, state="readonly", width=34
        )
        self.practice_type.current(0)
        self.practice_type.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 3))
        self.practice_type.bind("<<ComboboxSelected>>", self._on_type_change)

        ttk.Label(setup, text="2. Difficulty level", style="Dark.TLabel").grid(
            row=1, column=0, sticky="w", padx=8, pady=3
        )
        self.difficulty = ttk.Combobox(
            setup, values=DIFFICULTIES, state="readonly", width=34
        )
        self.difficulty.current(0)
        self.difficulty.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=3)

        self.drill_level_label = ttk.Label(
            setup, text="3. Drill level (guessing drill only)", style="Dark.TLabel"
        )
        self.drill_level = ttk.Combobox(
            setup, values=DRILL_LEVELS, state="readonly", width=34
        )
        self.drill_level.current(0)

        btn_row = ttk.Frame(setup, style="Dark.TFrame")
        btn_row.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(10, 8))
        start_btn = ttk.Button(btn_row, text="Start Practice", command=self.start_practice)
        start_btn.pack(side="left")
        new_btn = ttk.Button(btn_row, text="New Session (clear chat)", command=self.new_session)
        new_btn.pack(side="left", padx=(8, 0))

        input_outer = tk.Frame(page, bg=BG)
        input_outer.pack(side="bottom", fill="x", padx=10, pady=(0, 12))

        input_frame = tk.Frame(
            input_outer, bg=INPUT_BG, highlightthickness=1,
            highlightbackground=SIDEBAR_BORDER, highlightcolor=SIDEBAR_BORDER,
        )
        input_frame.pack(fill="x")

        self.entry = tk.Text(
            input_frame, height=3, wrap="word", font=("Sans", 11),
            bg=INPUT_BG, fg=FG, insertbackground=FG, relief="flat", undo=True,
            padx=10, pady=8, borderwidth=0, highlightthickness=0,
        )
        self.entry.pack(fill="x", side="top", padx=2, pady=(2, 0))
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Shift-Return>", self._on_shift_enter)
        add_select_all(self.entry)
        add_context_menu(self.entry, editable=True)
        add_paste_override(self.entry)
        add_undo_redo(self.entry)

        entry_bottom_row = tk.Frame(input_frame, bg=INPUT_BG)
        entry_bottom_row.pack(fill="x", padx=8, pady=(0, 6))

        self.provider_chip = tk.Label(
            entry_bottom_row, text=self._resolve_provider().split(" ")[0],
            bg=SIDEBAR_HOVER, fg=MUTED, font=("Sans", 8, "bold"), padx=8, pady=3,
        )
        self.provider_chip.pack(side="left")

        convo = tk.Frame(page, bg=BG)
        convo.pack(fill="both", expand=True, padx=6, pady=(0, 10))

        self.chat_area = tk.Text(
            convo, wrap="word", bg=BG, fg=FG, insertbackground=FG,
            font=("Sans", 11), state="disabled", padx=16, pady=14,
            relief="flat", borderwidth=0, height=10, spacing2=2,
        )
        self.chat_area.pack(fill="both", expand=True, padx=6, pady=6)
        self.chat_area.tag_config(
            "user", foreground=FG, background=BUBBLE_BG,
            lmargin1=60, lmargin2=60, rmargin=10,
            spacing1=8, spacing3=12,
        )
        self.chat_area.tag_config(
            "coach", foreground=FG, justify="left",
            lmargin1=2, lmargin2=2, rmargin=100,
            spacing1=4, spacing3=16,
        )
        self.chat_area.tag_config(
            "system", foreground=MUTED, justify="center",
            font=("Sans", 9, "italic"), spacing1=4, spacing3=12,
        )
        add_select_all(self.chat_area)
        add_context_menu(self.chat_area, editable=False)

        send_btn = tk.Label(
            entry_bottom_row, text="↑", bg=SEND_BTN_BG, fg="#0d0d0d",
            font=("Sans", 12, "bold"), width=3, cursor="hand2",
        )
        send_btn.pack(side="right")
        send_btn.bind("<Button-1>", lambda e: self.send_message())

    def _toggle_setup_panel(self):
        self.setup_collapsed = not self.setup_collapsed
        if self.setup_collapsed:
            self.setup_body.pack_forget()
            self.setup_header_label.config(text="▸  Session Setup (collapsed)")
        else:
            self.setup_body.pack(fill="x", padx=4, pady=(0, 6))
            self.setup_header_label.config(text="▾  Session Setup")

    def _on_type_change(self, event):
        if self.practice_type.get() == "Step-by-step guessing drill":
            self.drill_level_label.grid(row=2, column=0, sticky="w", padx=8, pady=3)
            self.drill_level.grid(row=2, column=1, sticky="w", padx=(0, 8), pady=3)
        else:
            self.drill_level_label.grid_remove()
            self.drill_level.grid_remove()

    def _on_enter(self, event):
        self.send_message()
        return "break"

    def _on_shift_enter(self, event):
        self.entry.insert("insert", "\n")
        self.entry.see("insert")
        return "break"

    def _append(self, tag, text):
        self.chat_area.config(state="normal")
        start_index = self.chat_area.index("end-1c")
        self.chat_area.insert("end", text + "\n\n", tag)
        self.chat_area.config(state="disabled")
        # Scroll to the START of the new message, not the end — for a long
        # message, jumping to "end" would put the user at the bottom with
        # no way to read from the top without scrolling back up.
        self.chat_area.see(start_index)

    def _resolve_api_key(self):
        return os.environ.get("OPENROUTER_API_KEY") or self.config_data.get("api_key")

    def _resolve_model(self):
        return self.config_data.get("model") or DEFAULT_MODEL

    def _resolve_provider(self):
        return self.config_data.get("provider") or PROVIDERS[0]

    def _resolve_claude_model(self):
        return self.config_data.get("claude_model") or DEFAULT_CLAUDE_CLI_MODEL

    def _resolve_claude_effort(self):
        return self.config_data.get("claude_effort") or DEFAULT_CLAUDE_CLI_EFFORT

    def new_session(self):
        self.messages = []
        self.claude_session_id = None
        self.current_chat_id = None
        self.chat_area.config(state="normal")
        self.chat_area.delete("1.0", "end")
        self.chat_area.config(state="disabled")
        self._refresh_chats_sidebar()

    def start_practice(self):
        if not self.system_prompt:
            self._append("system", f"Could not load {NVC_DOC_PATH}")
            return
        practice_type = self.practice_type.get()
        difficulty = self.difficulty.get()
        drill_level = self.drill_level.get() if practice_type == "Step-by-step guessing drill" else None
        setup = f"[Practice setup] Practice type: {practice_type}. Difficulty: {difficulty}."
        if drill_level:
            setup += f" Drill level: {drill_level}."
        setup += (
            " Begin the practice session accordingly, following your "
            "instructions — I've already selected what I want to "
            "practice and my difficulty level here, so there's no need "
            "to ask me for them."
        )
        self.current_practice_type = practice_type
        self.current_difficulty = difficulty
        self.current_drill_level = drill_level
        self.new_session()
        self.current_chat_id = uuid.uuid4().hex[:12]
        self.messages.append({"role": "user", "content": setup})
        self._append("system", f"Starting: {practice_type} ({difficulty})")
        if not self.setup_collapsed:
            self._toggle_setup_panel()
        self._call_model()

    def send_message(self):
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        self.entry.delete("1.0", "end")
        self._append("user", text)
        self.messages.append({"role": "user", "content": text})
        self._call_model()

    def _call_model(self):
        provider = self._resolve_provider()
        if provider == PROVIDERS[1]:
            if not claude_cli_available():
                self._append(
                    "system",
                    "The `claude` CLI was not found on this machine's PATH. "
                    "Install Claude Code and run `claude login` first, or "
                    "switch back to OpenRouter in Settings.",
                )
                return
            threading.Thread(target=self._call_model_thread, daemon=True).start()
        else:
            api_key = self._resolve_api_key()
            if not api_key:
                self._append(
                    "system",
                    "No OpenRouter API key found. Set OPENROUTER_API_KEY, or "
                    "add one in the Settings tab.",
                )
                return
            threading.Thread(target=self._call_model_thread, args=(api_key,), daemon=True).start()

    def _call_model_thread(self, api_key=None):
        provider = self._resolve_provider()
        if provider == PROVIDERS[1]:
            reply = self._call_claude_cli()
        else:
            reply = self._call_openrouter(api_key)

        if self.current_practice_type == "Step-by-step guessing drill":
            reply = strip_emphasis_from_numbered_lines(reply)

        self.messages.append({"role": "assistant", "content": reply})
        self._save_current_chat()
        self.after(0, lambda: self._append("coach", reply))
        self.after(0, self._refresh_chats_sidebar)

    def _save_current_chat(self):
        if not self.current_chat_id:
            return
        now = datetime.datetime.now()
        existing = next(
            (c for c in list_chats() if c["id"] == self.current_chat_id), None
        )
        title = (existing or {}).get("title") or (
            f"{self.current_practice_type} · {now.strftime('%b %d, %I:%M %p')}"
        )
        chat = {
            "id": self.current_chat_id,
            "title": title,
            "practice_type": self.current_practice_type,
            "difficulty": self.current_difficulty,
            "drill_level": self.current_drill_level,
            "provider": self._resolve_provider(),
            "claude_session_id": self.claude_session_id,
            "messages": self.messages,
            "created_at": (existing or {}).get("created_at") or now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"),
        }
        save_chat(chat)

    def _call_openrouter(self, api_key):
        model = self._resolve_model()
        full_messages = [{"role": "system", "content": self.system_prompt}] + self.messages
        try:
            return call_openrouter(api_key, model, full_messages)
        except urllib.error.HTTPError as e:
            return f"[HTTP error {e.code}: {e.read().decode('utf-8', 'ignore')}]"
        except urllib.error.URLError as e:
            return f"[Network error: {e.reason}]"
        except Exception as e:
            return f"[Error: {e}]"

    def _call_claude_cli(self):
        # The CLI's own --resume session tracks history, so only the latest
        # message needs to be sent — not the full self.messages transcript.
        prompt = self.messages[-1]["content"] if self.messages else ""
        model = self._resolve_claude_model()
        effort = self._resolve_claude_effort()
        try:
            if self.claude_session_id:
                data = call_claude_cli(prompt, model, effort, session_id=self.claude_session_id)
            else:
                data = call_claude_cli(prompt, model, effort, system_prompt=self.system_prompt)
        except subprocess.TimeoutExpired:
            return "[claude CLI timed out]"
        except Exception as e:
            return f"[Claude CLI error: {e}]"
        self.claude_session_id = data.get("session_id") or self.claude_session_id
        return data.get("result", "")

    # ---------------- Learning Library tab ----------------

    def _build_learn_tab(self, parent):
        page = ttk.Frame(parent, style="Dark.TFrame")
        self.pages["learn"] = page

        paned = ttk.PanedWindow(page, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(paned, style="Panel.TFrame", width=260)
        right = ttk.Frame(paned, style="Dark.TFrame")
        paned.add(left, weight=1)
        paned.add(right, weight=3)

        ttk.Label(left, text="Topics", style="Section.TLabel").pack(
            anchor="w", padx=8, pady=(8, 4)
        )

        tree_frame = ttk.Frame(left, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        for node in LEARN_TREE:
            self._insert_learn_node(node, "")

        self.tree.bind("<<TreeviewSelect>>", self._on_learn_select)

        self.learn_title = ttk.Label(right, text="", style="Title.TLabel")
        self.learn_title.pack(anchor="w", padx=10, pady=(4, 6))

        text_frame = ttk.Frame(right, style="Dark.TFrame")
        text_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.learn_text = tk.Text(
            text_frame, wrap="word", bg=BG, fg=FG, font=("Sans", 10),
            relief="flat", borderwidth=0, padx=6, pady=4, cursor="arrow",
        )
        learn_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.learn_text.yview)
        self.learn_text.configure(yscrollcommand=learn_scroll.set)
        learn_scroll.pack(side="right", fill="y")
        self.learn_text.pack(side="left", fill="both", expand=True)

        self.learn_text.tag_configure("para", foreground=FG, spacing3=10)
        self.learn_text.tag_configure(
            "subheading", foreground=ACCENT, font=("Sans", 10, "bold"), spacing1=6, spacing3=4,
        )
        self.learn_text.tag_configure(
            "quote", foreground=ACCENT, font=("Sans", 10, "italic"), spacing1=4,
        )
        self.learn_text.tag_configure("note", foreground=FG, spacing1=2, spacing3=10)
        self.learn_text.configure(state="disabled")

        first = LEARN_TREE[0]
        self.tree.selection_set(first["id"])
        self._render_learn_node(first)

    def _insert_learn_node(self, node, parent_iid):
        self.learn_nodes[node["id"]] = node
        self.tree.insert(parent_iid, "end", iid=node["id"], text=node["label"], open=True)
        for child in node.get("children", []):
            self._insert_learn_node(child, node["id"])

    def _on_learn_select(self, event):
        selection = self.tree.selection()
        if not selection:
            return
        node = self.learn_nodes.get(selection[0])
        if node:
            self._render_learn_node(node)

    def _render_learn_node(self, node):
        self.learn_title.config(text=node["label"])
        self.learn_text.configure(state="normal")
        self.learn_text.delete("1.0", "end")
        for block in node["content"]:
            kind = block[0]
            if kind == "para":
                self.learn_text.insert("end", block[1] + "\n\n", "para")
            elif kind == "subheading":
                self.learn_text.insert("end", block[1] + "\n", "subheading")
            elif kind == "bullets":
                for item in block[1]:
                    self.learn_text.insert("end", "• " + item + "\n", "para")
                self.learn_text.insert("end", "\n", "para")
            elif kind == "example":
                self.learn_text.insert("end", block[1] + "\n", "quote")
                self.learn_text.insert("end", "→ " + block[2] + "\n\n", "note")
        self.learn_text.configure(state="disabled")

    # ---------------- Settings tab ----------------

    def _build_settings_tab(self, parent):
        page = ttk.Frame(parent, style="Dark.TFrame")
        self.pages["settings"] = page

        provider_box = ttk.LabelFrame(page, text="AI Provider", style="Dark.TLabelframe")
        provider_box.pack(fill="x", padx=16, pady=(16, 0))

        ttk.Label(provider_box, text="1. Choose provider", style="Dark.TLabel").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 3)
        )
        self.provider_select = ttk.Combobox(
            provider_box, values=PROVIDERS, state="readonly", width=40
        )
        current_provider = self._resolve_provider()
        self.provider_select.set(current_provider)
        self.provider_select.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 3))
        self.provider_select.bind("<<ComboboxSelected>>", self._on_provider_change)

        claude_note = (
            "Claude Code (local CLI) shells out to the `claude` command in "
            "\"print\" mode and authenticates via `claude login` — no API "
            "key, billed against your Claude Pro/Max/Team/Enterprise "
            "subscription usage instead of pay-per-token."
        )
        ttk.Label(provider_box, text=claude_note, style="Muted.TLabel", wraplength=560).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 10)
        )

        conn = ttk.LabelFrame(page, text="OpenRouter Connection", style="Dark.TLabelframe")
        conn.pack(fill="x", padx=16, pady=16)
        self.openrouter_box = conn

        env_key = os.environ.get("OPENROUTER_API_KEY")
        env_note = (
            "OPENROUTER_API_KEY is set in your environment and will be used automatically."
            if env_key else
            "OPENROUTER_API_KEY is not set in your environment — you can save a key below instead."
        )
        ttk.Label(conn, text=env_note, style="Muted.TLabel", wraplength=560).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 10)
        )

        ttk.Label(conn, text="API key (used only if the env var above is not set):", style="Dark.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8
        )

        key_row = ttk.Frame(conn, style="Dark.TFrame")
        key_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 2))

        self.api_key_entry = tk.Entry(
            key_row, width=44, show="*", bg=INPUT_BG, fg=FG, insertbackground=FG,
            relief="flat", readonlybackground=INPUT_BG,
        )
        self.api_key_entry.insert(0, self.config_data.get("api_key", ""))
        self.api_key_entry.configure(state="readonly")
        self.api_key_entry.pack(side="left")
        add_select_all(self.api_key_entry)
        add_context_menu(self.api_key_entry, editable=True)
        add_paste_override(self.api_key_entry)

        self.api_key_edit_btn = ttk.Button(key_row, text="Edit", command=self._toggle_api_key_lock)
        self.api_key_edit_btn.pack(side="left", padx=(6, 0))

        ttk.Label(
            conn, text="Locked by default — click Edit to unlock before changing it.",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 10))

        ttk.Label(conn, text="Model slug:", style="Dark.TLabel").grid(row=4, column=0, sticky="w", padx=8)
        self.model_entry = tk.Entry(
            conn, width=50, bg=INPUT_BG, fg=FG, insertbackground=FG, relief="flat"
        )
        self.model_entry.insert(0, self.config_data.get("model", DEFAULT_MODEL))
        self.model_entry.grid(row=5, column=0, sticky="w", padx=8, pady=(2, 12))
        add_select_all(self.model_entry)
        add_context_menu(self.model_entry, editable=True)
        add_paste_override(self.model_entry)

        note = (
            "Get a key at openrouter.ai/keys. Usage is billed per token by "
            "OpenRouter — separate from any Claude.ai subscription. The "
            "key you save here is stored in plain text at "
            f"{CONFIG_PATH} (file permissions set to owner-only)."
        )
        ttk.Label(conn, text=note, style="Muted.TLabel", wraplength=560).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 10)
        )

        claude_box = ttk.LabelFrame(page, text="Claude Code (local CLI)", style="Dark.TLabelframe")
        claude_box.pack(fill="x", padx=16, pady=(0, 16))
        self.claude_box = claude_box

        cli_status = (
            "`claude` was found on this machine's PATH."
            if claude_cli_available() else
            "`claude` was NOT found on this machine's PATH — install Claude "
            "Code and run `claude login` before selecting this provider."
        )
        ttk.Label(claude_box, text=cli_status, style="Muted.TLabel", wraplength=560).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 10)
        )

        ttk.Label(claude_box, text="Model:", style="Dark.TLabel").grid(
            row=1, column=0, sticky="w", padx=8
        )
        self.claude_model_select = ttk.Combobox(
            claude_box, values=CLAUDE_CLI_MODELS, state="readonly", width=20
        )
        self.claude_model_select.set(self.config_data.get("claude_model", DEFAULT_CLAUDE_CLI_MODEL))
        self.claude_model_select.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=(2, 8))

        ttk.Label(claude_box, text="Reasoning effort:", style="Dark.TLabel").grid(
            row=2, column=0, sticky="w", padx=8
        )
        self.claude_effort_select = ttk.Combobox(
            claude_box, values=CLAUDE_CLI_EFFORTS, state="readonly", width=20
        )
        self.claude_effort_select.set(self.config_data.get("claude_effort", DEFAULT_CLAUDE_CLI_EFFORT))
        self.claude_effort_select.grid(row=2, column=1, sticky="w", padx=(0, 8), pady=(2, 10))

        save_btn = ttk.Button(page, text="Save Settings", command=self._save_settings)
        save_btn.pack(anchor="w", padx=16, pady=(0, 4))

        self.settings_status = ttk.Label(page, text="", style="Muted.TLabel")
        self.settings_status.pack(anchor="w", padx=16, pady=(0, 16))

        self._update_provider_visibility()

    def _on_provider_change(self, event):
        self._update_provider_visibility()

    def _update_provider_visibility(self):
        if self.provider_select.get() == PROVIDERS[1]:
            self.openrouter_box.pack_forget()
            self.claude_box.pack(fill="x", padx=16, pady=(0, 16))
        else:
            self.claude_box.pack_forget()
            self.openrouter_box.pack(fill="x", padx=16, pady=16)

    def _toggle_api_key_lock(self):
        if str(self.api_key_entry["state"]) == "readonly":
            self.api_key_entry.configure(state="normal")
            self.api_key_edit_btn.configure(text="Lock")
        else:
            self.api_key_entry.configure(state="readonly")
            self.api_key_edit_btn.configure(text="Edit")

    def _save_settings(self):
        self.config_data["provider"] = self.provider_select.get()
        self.config_data["api_key"] = self.api_key_entry.get().strip()
        self.config_data["model"] = self.model_entry.get().strip() or DEFAULT_MODEL
        self.config_data["claude_model"] = self.claude_model_select.get() or DEFAULT_CLAUDE_CLI_MODEL
        self.config_data["claude_effort"] = self.claude_effort_select.get() or DEFAULT_CLAUDE_CLI_EFFORT
        save_config(self.config_data)
        self.api_key_entry.configure(state="readonly")
        self.api_key_edit_btn.configure(text="Edit")
        self.settings_status.config(text="Saved and locked.")
        self.footer_label.config(text=self._resolve_provider())
        self.provider_chip.config(text=self._resolve_provider().split(" ")[0])


if __name__ == "__main__":
    app = App()
    app.mainloop()
