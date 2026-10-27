"""Traducao da interface: portugues e ingles.

Mesmo mecanismo do Artemis: um dicionario plano por idioma, com chaves
pontuadas, resolvido na hora da chamada. Faltando uma chave, cai para o
ingles; faltando no ingles tambem, devolve a propria chave. Uma traducao
incompleta deixa a tela feia, nunca quebra o app.

Para adicionar um idioma: copie o bloco "en", traduza os valores e registre
o codigo em LANGUAGE_NAMES.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

log = logging.getLogger(__name__)

DEFAULT = "en"
AUTO = "auto"

# Cada idioma aparece com o proprio nome.
LANGUAGE_NAMES = {
    AUTO: "Automático / Automatic",
    "pt": "Português (Brasil)",
    "en": "English",
}

# LANGID primario do Windows -> nosso codigo.
_WINDOWS_PRIMARY = {0x16: "pt", 0x09: "en"}

_current = DEFAULT


def detect() -> str:
    """Idioma da interface do sistema, ou o padrao se nao for suportado."""
    if sys.platform == "win32":
        try:
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return _WINDOWS_PRIMARY.get(langid & 0x3FF, DEFAULT)
        except Exception:
            return DEFAULT
    # No Linux o idioma esta no ambiente. LANGUAGE vem primeiro e pode trazer
    # uma lista de preferencias ("pt_BR:pt:en"); os demais trazem um locale
    # so ("pt_BR.UTF-8").
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if not value or value in ("C", "POSIX"):
            continue
        for item in value.split(":"):
            code = item.split(".")[0].split("@")[0].split("_")[0].lower()
            if code in _TRANSLATIONS:
                return code
    return DEFAULT


def set_language(code: str | None) -> str:
    """Define o idioma corrente. 'auto' segue o sistema. Devolve o efetivo."""
    global _current
    if not code or code == AUTO:
        _current = detect()
    elif code in _TRANSLATIONS:
        _current = code
    else:
        log.warning("Idioma '%s' desconhecido; usando '%s'.", code, DEFAULT)
        _current = DEFAULT
    return _current


def current() -> str:
    return _current


def t(key: str, **kwargs) -> str:
    """Texto traduzido. kwargs preenchem os campos {} da string."""
    text = _TRANSLATIONS.get(_current, {}).get(key)
    if text is None:
        text = _TRANSLATIONS[DEFAULT].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text  # traducao com placeholder errado nao derruba a tela
    return text


_TRANSLATIONS: dict[str, dict[str, str]] = {}

# --------------------------------------------------------------- English

_TRANSLATIONS["en"] = {
    # -- linha de comando
    "cli.set_key.prompt": "Paste your OpenAI API key (it will not be shown) and press Enter.",
    "cli.set_key.cancel": "An empty Enter cancels.",
    "cli.set_key.label": "API key: ",
    "cli.set_key.cancelled": "Cancelled, nothing was changed.",
    "cli.set_key.saved": "Saved to the Credential Manager: {masked}",
    "cli.already_running": "Apolo is already running (check the tray icon).",
    "cli.already_running.opened": "Apolo is already running; opened its settings window.",
    # -- estados
    "status.ready": "Ready",
    "status.preparing": "Preparing...",
    "status.recording": "Recording",
    "status.processing": "Processing...",
    "status.done": "Done",
    "status.error": "Error",
    "status.preparing.detail": "Opening the microphone and the system audio. Wait for 'Recording' before you speak.",
    "status.draining": "Transcribing the rest...",
    "status.note_saved": "Note saved",
    "status.pending": "{n} chunk(s) waiting ({seconds}s of audio)",
    "status.offline": "no connection - the audio waits on disk",
    "status.retrying": "OpenAI unstable, retrying",
    "status.channel_down.mic": "Your microphone is not being recorded.",
    "status.channel_down.sys": "The system audio (others) is not being recorded.",
    "status.audio_kept": "The audio not yet transcribed stays on disk.",
    "status.waiting_choice": "Choose the summary type to finish.",
    "status.summarizing": "Writing the summary...",
    "status.saved_without_summary": "Note saved without a summary",
    "note.summary.nothing": "Nothing was transcribed in this meeting.",
    "note.summary.failed": "The summary could not be generated ({error}). Use 'Summarise again' in the tray menu.",
    "err.note_not_apolo": "This file is not an Apolo note.",
    "err.note_read": "Could not read the note.",
    "stop.ended": "Meeting ended ({duration})",
    "stop.hint": "The rest of the transcript finishes in the background while you fill this in. Closing the window uses what is selected.",
    "stop.type": "Summary type",
    "stop.title": "Title",
    "stop.people": "Who was there",
    "stop.people.hint": "Optional. Names separated by commas.",
    "stop.generate": "Generate summary",
    "resummarize.title": "Summarise again: {name}",
    "resummarize.hint": "Rewrites the summary of this note from its transcript. Your notes and the transcript stay as they are.",
    "resummarize.button": "Summarise again",
    "tray.resummarize": "Summarise again...",
    "recover.text": (
        "A recording from {when} did not finish ({pending} chunk(s) of audio "
        "still to transcribe).\n\n"
        "Yes: finish it now (transcribe what is left and summarise).\n"
        "No: discard the leftover audio.\n"
        "Cancel: ask again next time."
    ),
    "recover.confirm_discard": "Discard the leftover audio of that recording? The note keeps what was already transcribed.",
    "status.mic_paused": "Your microphone is paused (not being transcribed).",
    "status.silent": "No speech for {minutes} min - forgot to stop?",
    "status.limit_reached": "Recording for {hours} h: stopping on its own and writing the note.",
    "tray.pause_mic": "Pause my microphone",
    "cfg.tab.audio": "  Audio  ",
    "cfg.tab.notes": "  Notes  ",
    "cfg.tab.presets": "  Summary types  ",
    "cfg.clear": "Clear",
    "cfg.system_default": "(system default)",
    "cfg.section.hotkeys": "Shortcuts",
    "cfg.hotkey": "Start / stop",
    "cfg.hotkey.hint": "Optional. The tray menu always works. Click Capture and press the combination.",
    "cfg.pause_hotkey": "Pause my mic",
    "cfg.pause_hotkey.hint": "Optional. Muted in the meeting? Pause here too, so side conversations are not transcribed.",
    "cfg.capture": "Capture",
    "cfg.capture.title": "Capture shortcut",
    "cfg.capture.prompt": "Press the combination you want.",
    "cfg.capture.escape": "Esc cancels.",
    "cfg.capture.hint": "Hold every key, then release them all to confirm.",
    "cfg.section.devices": "Devices",
    "cfg.mic_device": "Microphone (Me)",
    "cfg.mic_device.hint": "Your voice. With Bluetooth headphones, recording starts ~1.5 s after you click Start.",
    "cfg.loopback_device": "Output (Others)",
    "cfg.loopback_device.hint": "Whatever plays on this output becomes 'Others'. The default follows Windows: if you plug in headphones mid-meeting, Apolo moves along.",
    "cfg.section.test": "Test",
    "cfg.meter.mic": "Microphone",
    "cfg.meter.sys": "System audio",
    "cfg.test": "Test for 5 s",
    "cfg.test.running": "Speak, and play something (a video, a call) on the output...",
    "cfg.test.busy": "Stop the recording before testing the devices.",
    "cfg.test.line": "{channel}: '{device}' - first audio after {first}, peak {peak} dBFS",
    "cfg.test.error": "{channel}: {error}",
    "cfg.test.hint": "Both bars should move. If the microphone bar also moves while only the output plays, you are on speakers: use headphones, or the others' voices end up as 'Me'.",
    "cfg.section.ending": "When the meeting ends",
    "cfg.default_preset": "Default summary type",
    "cfg.default_preset.hint": "Used by 'Start recording' in the tray and by the shortcut.",
    "cfg.on_stop": "On stop",
    "cfg.on_stop.ask": "Ask type, title and participants",
    "cfg.on_stop.auto": "Summarise right away with the default type",
    "cfg.on_stop.hint": "The window opens while the transcript finishes, so it adds no waiting.",
    "cfg.transcript_block": "Transcript format",
    "cfg.transcript_block.callout": "Obsidian callout (collapsible)",
    "cfg.transcript_block.details": "HTML <details> (VS Code, GitHub)",
    "cfg.transcript_block.hint": "How the transcript appears collapsed at the end of the note.",
    "cfg.section.save_audio": "Meeting audio",
    "cfg.save_audio": "Keep the meeting audio (one FLAC per channel)",
    "cfg.save_audio.hint": "Off by default: the audio only exists on disk until it is transcribed. On, it is kept - and it contains other people's voices.",
    "cfg.audio_dir": "Audio folder",
    "cfg.audio_dir.hint": "Empty = an 'audio' folder inside the notes folder.",
    "cfg.section.limits": "Safety limits",
    "cfg.silence_alert": "Silence alert (min)",
    "cfg.silence_alert.hint": "Warns when nobody has spoken for this long. 0 turns it off.",
    "cfg.max_hours": "Stop after (h)",
    "cfg.max_hours.hint": "Stops on its own and writes the note, without asking. 0 turns it off.",
    "cfg.preset.new": "New",
    "cfg.preset.remove": "Remove",
    "cfg.preset.new_name": "New type {n}",
    "cfg.preset.new_prompt": "Work meeting. Write the body with these sections:\n## Summary\n## Next steps",
    "cfg.preset.name": "Name",
    "cfg.preset.description": "Description",
    "cfg.preset.prompt": "Focus and sections",
    "cfg.preset.prompt.hint": "What this kind of meeting needs and which '##' sections the summary has. Language, format and 'do not make things up' are added on their own.",
    "cfg.preset.keywords": "Terms",
    "cfg.preset.keywords.hint": "One per line. Added to the global terms for meetings of this type.",
    "cfg.preset.languages": "Speech languages",
    "cfg.preset.languages.hint": "Empty uses the ones from the General tab.",
    "cfg.preset.model": "Summary model",
    "cfg.preset.model.hint": "Empty uses the global summary model.",
    "dlg.keep_one_preset": "At least one summary type must remain.",
    "dlg.remove_preset": "Remove the summary type '{name}'?",
    "dlg.preset_invalid": "Invalid summary type: {error}",
    "dlg.hotkey_same": "The two shortcuts are the same.",
    # -- bandeja
    "tray.tooltip": "Apolo",
    "tray.toggle.start": "Start recording ({name})",
    "tray.toggle.stop": "Stop recording",
    "tray.toggle.wait": "Please wait...",
    "tray.start_with": "Start with summary type",
    "tray.open_last_note": "Open last note",
    "tray.open_notes": "Open notes folder",
    "tray.settings": "Settings...",
    "tray.quit": "Quit",
    "tray.notify.no_key": "Set your OpenAI API key to get started.",
    "tray.notify.no_tray": "This session has no system tray, so the icon has nowhere to appear. Open Apolo from the applications menu to reach the settings.",
    # -- erros
    "err.no_api_key": "OpenAI API key is not set.",
    "err.no_api_key.detail": "Open the Apolo settings from the tray icon.",
    "err.no_api_key.cli": "Open the Apolo settings, or set OPENAI_API_KEY.",
    "err.invalid_api_key": "OpenAI API key is invalid or revoked.",
    "err.invalid_api_key.detail": "Check the key in the Apolo settings.",
    "err.no_model_access": "Your account cannot access this model.",
    "err.rate_limit": "OpenAI usage limit reached. Retrying shortly.",
    "err.timeout": "OpenAI took too long to answer.",
    "err.offline": "No connection to OpenAI.",
    "err.offline.detail": "Pending audio waits on disk until the connection comes back.",
    "err.api_status": "OpenAI returned error {status}.",
    "err.unexpected": "Unexpected failure.",
    "err.hotkey_invalid": "Invalid shortcut: '{spec}'.",
    "err.hotkey_invalid.detail": "Use the pynput format, e.g. <ctrl>+<alt>+m.",
    "err.hotkey_empty": "Empty shortcut for '{mode}'.",
    "err.hotkey_conflict": "The shortcut '{spec}' is registered twice ('{first}' and '{second}').",
    "err.hotkey_problems": "Shortcut problems",
    "err.config_read": "Could not read {file}. Fix it or delete the file.",
    "err.config_object": "config.json should contain a JSON object.",
    "err.presets_list": "presets.json should contain a non-empty list.",
    "err.preset_invalid": "Invalid preset in presets.json.",
    "err.keyring_save": "Could not save the API key to the Credential Manager.",
    "err.startup_write": "Could not change the startup entry.",
    "err.notes_dir": "Could not create the notes folder.",
    "err.note_write": "Could not write the note.",
    "err.note_open": "Could not open the note.",
    "err.no_audio": "No audio arrived from the microphone or the system.",
    "err.no_presets": "No summary types configured.",
    # -- janela de configuracao
    "cfg.title": "Apolo - Settings",
    "cfg.tab.general": "  General  ",
    "cfg.save": "Save",
    "cfg.cancel": "Cancel",
    "cfg.browse": "Choose...",
    "cfg.section.openai": "OpenAI",
    "cfg.section.transcription": "Transcription",
    "cfg.section.notes": "Notes",
    "cfg.section.interface": "Interface",
    "cfg.section.behavior": "Behaviour",
    "cfg.section.files": "Configuration files",
    "cfg.api_key": "API key",
    "cfg.api_key.hint": "Stored in the Credential Manager. Current: {current}. Leave blank to keep it.",
    "cfg.api_key.hint_artemis": "Stored in the Credential Manager. Using the Artemis Dictation key ({current}) because Apolo has none of its own. Fill this in to use a different one.",
    "cfg.api_key.hint_env": "Using OPENAI_API_KEY from the environment ({current}). Fill this in to store a key in the Credential Manager.",
    "cfg.api_key.none": "(none)",
    "cfg.stt_model": "Transcription",
    "cfg.stt_model.hint": "gpt-transcribe accepts several languages and a vocabulary list.",
    "cfg.text_model": "Summary",
    "cfg.text_model.hint": "Model that writes the summary when a meeting ends. Each summary type can override it.",
    "cfg.languages": "Speech languages",
    "cfg.languages.hint": "Two-letter codes separated by commas. 'pt, en' handles Portuguese with English terms mixed in. Empty lets the model detect it.",
    "cfg.keywords": "Vocabulary",
    "cfg.keywords.hint": "One per line. Names the transcription tends to get wrong: people, clients, products, jargon.",
    "cfg.my_name": "Your name",
    "cfg.my_name.hint": "Who 'Me' is in the transcript, so action items come out with your name.",
    "cfg.notes_dir": "Notes folder",
    "cfg.notes_dir.hint": "Where meeting notes (.md) are written. It can be an Obsidian vault folder.",
    "cfg.ui_language": "Interface language",
    "cfg.ui_language.hint": "Changes menus, settings and messages. 'Automatic' follows the system.",
    "cfg.overlay": "On-screen indicator",
    "cfg.overlay.always": "Always show",
    "cfg.overlay.errors": "Only on errors",
    "cfg.overlay.never": "Never show",
    "cfg.overlay.hint": "The floating indicator in the bottom-right corner, with the recording time. The tray icon changes colour either way.",
    "cfg.autostart": "Start Apolo with the system",
    "cfg.autostart.hint": "Adds an entry under the current user only - no administrator rights needed.",
    "cfg.autostart.frozen_hint": "Registered command: {command}",
    "cfg.open_folder": "Open folder",
    "cfg.reload": "Reload from disk",
    "cfg.files.hint": "Use 'Reload' after editing config.json or presets.json by hand. The log is in apolo.log, in the same folder.",
    # -- dialogos
    "dlg.save_failed": "Could not save: {error}",
    # -- desktop (Linux)
    "app.tagline": "Local meeting notes",
    "desktop.settings": "Settings",
    "desktop.keywords": "meeting;notes;transcription;summary;",
    # -- presets padrao
    "preset.geral.name": "General",
    "preset.geral.description": "Any meeting: summary, decisions, next steps.",
    "preset.geral.system_prompt": (
        "General work meeting. Write the body with these sections, in this "
        "order:\n"
        "## Summary - 3 to 6 sentences: what the meeting was about and where "
        "it landed.\n"
        "## Decisions - what was decided, one bullet each.\n"
        "## Next steps - task list in the format '- [ ] **Owner**: task (due "
        "date, if mentioned)'.\n"
        "## Open questions - what was raised and left unresolved.\n"
        "Leave a section out entirely if there is nothing for it."
    ),
    "preset.daily.name": "Daily",
    "preset.daily.description": "Stand-up: who did what, plans and blockers.",
    "preset.daily.system_prompt": (
        "Daily stand-up of a software team. Write the body with these "
        "sections, in this order:\n"
        "## Summary - 1 to 3 sentences on the overall state.\n"
        "## By person - one sub-item per person with: did / will do / "
        "blockers. Use the names you can identify from the conversation; "
        "group what you cannot attribute under 'Others'.\n"
        "## Blockers - every blocker, with who can unblock it.\n"
        "## Next steps - task list in the format '- [ ] **Owner**: task'.\n"
        "Be brief: a daily note is read in 30 seconds."
    ),
    "preset.cliente.name": "Client",
    "preset.cliente.description": "Meeting with a client: needs, agreements, commitments.",
    "preset.cliente.system_prompt": (
        "Meeting with a client or external partner. Write the body with these "
        "sections, in this order:\n"
        "## Context - who was there and the goal of the meeting.\n"
        "## Client needs and pain points - in the client's own words where "
        "possible.\n"
        "## Agreed - what was agreed, including scope, price and dates if "
        "mentioned.\n"
        "## Commitments - two lists: 'Ours' and 'Theirs'.\n"
        "## Risks - risks and points of attention you noticed.\n"
        "## Next steps - task list in the format '- [ ] **Owner**: task (due "
        "date)'.\n"
        "## Follow-up email draft - a short, cordial email recapping the "
        "agreements and next steps, ready to send.\n"
        "Leave a section out entirely if there is nothing for it."
    ),
    "preset.1on1.name": "1:1",
    "preset.1on1.description": "One-on-one: topics, feedback and agreements.",
    "preset.1on1.system_prompt": (
        "One-on-one meeting between two people (manager and report, or "
        "peers). Write the body with these sections, in this order:\n"
        "## Topics - what was discussed, one bullet each.\n"
        "## Feedback - feedback given and received, in both directions.\n"
        "## Agreements - task list in the format '- [ ] **Owner**: task'.\n"
        "## For the next 1:1 - what to follow up on.\n"
        "Keep a respectful, neutral tone; do not judge people."
    ),
    # -- audio (fase 2)
    "err.vad_load": "Could not load the voice detector (Silero VAD).",
    "err.mic_list": "Could not list the microphones.",
    "err.mic_open": "Could not open the microphone.",
    "err.mic_busy": "The microphone is busy or unavailable.",
    "err.mic_lost": "The microphone stopped delivering audio.",
    "err.loopback_list": "Could not list the audio outputs.",
    "err.loopback_open": "Could not capture the system audio (loopback).",
    "err.loopback_lost": "System audio capture (loopback) stopped.",
    "err.audio_unexpected": "Unexpected failure in audio capture.",
    "err.spool_write": "Could not write the temporary audio to disk.",
    "err.spool_delete": "Could not delete the temporary audio.",
    "err.spool_missing": "Session not found in the audio spool.",
    "err.wav_read": "Could not read the WAV file.",
    "err.wav_format": "Unsupported WAV: use 16-bit PCM.",
    "cli.default": "default",
    "cli.devices.inputs": "Microphones (mic_device, channel \"Me\"):",
    "cli.devices.outputs": "Outputs whose loopback becomes \"Others\" (loopback_device):",
    "cli.devices.legend": "* = system default",
    "cli.devices.configured": "Configured: mic = {mic} | loopback = {loopback}",
    "cli.capture.preparing": "Preparing (the Bluetooth mic can take ~1.5 s)...",
    "cli.capture.no_first_block": "No audio from {channel} after {seconds:.0f}s.",
    "cli.capture.recording": "Recording for {seconds:.0f}s. Speak, and play something on the output. Ctrl+C stops.",
    "cli.capture.channel_error": "{channel} stopped: {error}",
    "cli.capture.channel_stats": "{channel}: '{device}' | first block after {first} | speech {speech:.1f}s | clock adjustments {resyncs}",
    "cli.capture.spool_kept": "Chunks kept for listening in {path}",
    "cli.capture.spool_deleted": "Temporary audio deleted (use --keep-spool to keep it).",
    "cli.chunks.header": "channel   start  duration  speech   level",
    "cli.chunks.summary": "{count} chunks, {sent:.1f}s of {total:.1f}s of audio ({pct:.0f}%)",
    "cli.chunks.none": "No chunks: no speech detected.",
    "cli.from_wav.file": "{name}: {seconds:.1f}s",
    "cli.simulate.note": "Live note: {path}",
    "cli.simulate.done": "Done. Note: {path}",
    # -- provider (fases 3 e 4)
    "err.quota": "Your OpenAI account is out of credit.",
    "err.quota.detail": "Check billing at platform.openai.com. Pending audio stays on disk.",
    "err.transcription_failed": "Transcription failed.",
    "err.summary_failed": "Could not generate the summary.",
    "err.summary_refused": "The model refused to summarise this meeting.",
    "err.summary_empty": "The model returned an empty summary.",
    # -- nota
    "speaker.me": "Me",
    "speaker.others": "Others",
    "note.recording_title": "Recording",
    "note.untitled": "Meeting",
    "note.tag": "meeting",
    "note.header.date_fmt": "%Y-%m-%d %H:%M",
    "note.header.recording": "recording",
    "note.header.with": "with {names}",
    "note.summary": "Summary",
    "note.summary.pending": "Generated when the recording stops.",
    "note.my_notes": "My notes",
    "note.my_notes.placeholder": "(whatever you write here during the meeting is kept and used in the summary)",
    "note.transcript": "Transcript",
    "note.transcript.live": "Transcript (live)",
    "note.transcript.full": "Full transcript ({duration})",
    "note.transcript.empty": "nothing transcribed yet",
    "note.gap": "not transcribed",
    "note.gap.reason.offline": "no connection",
    "note.gap.reason.overflow": "too much pending audio",
    "note.gap.reason.error": "transcription failed",
    # -- resumo (prompt comum a todos os presets)
    "summary.rules": (
        "You write meeting notes from an automatic transcript.\n"
        "Rules:\n"
        "- Write in the language that dominates the meeting. Keep technical "
        "terms in English exactly as they were spoken.\n"
        "- Do not make anything up: use only what is in the transcript and "
        "in the user's notes. If something is unclear, say it is unclear.\n"
        "- '{me}' is {my_name}, who recorded the meeting. Use that name for "
        "their action items.\n"
        "- '{others}' is the computer audio and may be several people. Infer "
        "names from the context (who is called by name, who answers) and from "
        "the participant list; when you cannot, do not attribute it to anyone.\n"
        "- The user's notes show what they found important: give them weight.\n"
        "- Lines marked '{gap}' are gaps in the recording. Do not try to fill "
        "in what was said during them.\n"
        "- Automatic transcription gets words wrong; fix them from context "
        "only when it is obvious."
    ),
    "summary.me_unknown": "the person who recorded the meeting",
    "summary.format.json": (
        "Answer with a JSON object containing:\n"
        "- title: short, specific title for the meeting (up to ~60 "
        "characters), without the date.\n"
        "- tags: 1 to 5 lowercase tags, no spaces (use hyphens), no '#'.\n"
        "- body_markdown: the body of the note in Markdown, with the sections "
        "requested above, starting at the first '##'. No title, no "
        "frontmatter."
    ),
    "summary.format.markdown": (
        "Answer in Markdown only. First line: the meeting title (short, "
        "without '#' and without the date). Then a blank line and the body, "
        "with the sections requested above, starting at the first '##'."
    ),
    "summary.ctx.date": "Date: {date}",
    "summary.ctx.duration": "Duration: {minutes} min",
    "summary.ctx.participants": "Participants: {names}",
    "summary.ctx.me": "'{me}' in the transcript: {name}",
    "summary.ctx.notes": "User's notes",
    "summary.ctx.no_notes": "(none)",
    "summary.ctx.transcript": "Transcript",
}

# ------------------------------------------------------------- Portugues

_TRANSLATIONS["pt"] = {
    "cli.set_key.prompt": "Cole a API key da OpenAI (não aparece na tela) e tecle Enter.",
    "cli.set_key.cancel": "Enter vazio cancela.",
    "cli.set_key.label": "API key: ",
    "cli.set_key.cancelled": "Cancelado, nada foi alterado.",
    "cli.set_key.saved": "Salva no Gerenciador de Credenciais: {masked}",
    "cli.already_running": "O Apolo já está rodando (veja o ícone na bandeja).",
    "cli.already_running.opened": "O Apolo já está rodando; abri a janela de configurações dele.",
    "status.ready": "Pronto",
    "status.preparing": "Preparando...",
    "status.recording": "Gravando",
    "status.processing": "Processando...",
    "status.done": "Pronto",
    "status.error": "Erro",
    "status.preparing.detail": "Abrindo o microfone e o áudio do sistema. Espere aparecer 'Gravando' para falar.",
    "status.draining": "Transcrevendo o restante...",
    "status.note_saved": "Nota salva",
    "status.pending": "{n} trecho(s) na fila ({seconds}s de áudio)",
    "status.offline": "sem conexão - o áudio espera no disco",
    "status.retrying": "OpenAI instável, tentando de novo",
    "status.channel_down.mic": "Seu microfone não está sendo gravado.",
    "status.channel_down.sys": "O áudio do sistema (outros) não está sendo gravado.",
    "status.audio_kept": "O áudio ainda não transcrito continua no disco.",
    "status.waiting_choice": "Escolha o tipo de resumo para terminar.",
    "status.summarizing": "Escrevendo o resumo...",
    "status.saved_without_summary": "Nota salva sem resumo",
    "note.summary.nothing": "Nada foi transcrito nesta reunião.",
    "note.summary.failed": "Não consegui gerar o resumo ({error}). Use 'Resumir de novo' no menu da bandeja.",
    "err.note_not_apolo": "Este arquivo não é uma nota do Apolo.",
    "err.note_read": "Não consegui ler a nota.",
    "stop.ended": "Reunião encerrada ({duration})",
    "stop.hint": "O resto da transcrição termina por trás enquanto você preenche. Fechar a janela usa o que estiver marcado.",
    "stop.type": "Tipo de resumo",
    "stop.title": "Título",
    "stop.people": "Quem estava",
    "stop.people.hint": "Opcional. Nomes separados por vírgula.",
    "stop.generate": "Gerar resumo",
    "resummarize.title": "Resumir de novo: {name}",
    "resummarize.hint": "Reescreve o resumo desta nota a partir da transcrição. Suas anotações e a transcrição continuam como estão.",
    "resummarize.button": "Resumir de novo",
    "tray.resummarize": "Resumir de novo...",
    "recover.text": (
        "Uma gravação de {when} não terminou ({pending} trecho(s) de áudio "
        "ainda sem transcrever).\n\n"
        "Sim: terminar agora (transcrever o que falta e resumir).\n"
        "Não: descartar o áudio que sobrou.\n"
        "Cancelar: perguntar de novo na próxima vez."
    ),
    "recover.confirm_discard": "Descartar o áudio que sobrou dessa gravação? A nota fica com o que já foi transcrito.",
    "status.mic_paused": "Seu microfone está pausado (não está sendo transcrito).",
    "status.silent": "Ninguém fala há {minutes} min - esqueceu de parar?",
    "status.limit_reached": "Gravando há {hours} h: parando sozinho e escrevendo a nota.",
    "tray.pause_mic": "Pausar meu microfone",
    "cfg.tab.audio": "  Áudio  ",
    "cfg.tab.notes": "  Notas  ",
    "cfg.tab.presets": "  Tipos de resumo  ",
    "cfg.clear": "Limpar",
    "cfg.system_default": "(padrão do sistema)",
    "cfg.section.hotkeys": "Atalhos",
    "cfg.hotkey": "Iniciar / parar",
    "cfg.hotkey.hint": "Opcional. O menu da bandeja sempre funciona. Clique em Capturar e pressione a combinação.",
    "cfg.pause_hotkey": "Pausar meu mic",
    "cfg.pause_hotkey.hint": "Opcional. Mutou na reunião? Pause aqui também, para conversa paralela não ser transcrita.",
    "cfg.capture": "Capturar",
    "cfg.capture.title": "Capturar atalho",
    "cfg.capture.prompt": "Pressione a combinação desejada.",
    "cfg.capture.escape": "Esc cancela.",
    "cfg.capture.hint": "Segure todas as teclas e solte-as para confirmar.",
    "cfg.section.devices": "Dispositivos",
    "cfg.mic_device": "Microfone (Eu)",
    "cfg.mic_device.hint": "A sua voz. Com fone Bluetooth, a gravação começa ~1,5 s depois do Iniciar.",
    "cfg.loopback_device": "Saída (Outros)",
    "cfg.loopback_device.hint": "O que toca nesta saída vira 'Outros'. O padrão segue o Windows: se você conectar o fone no meio da reunião, o Apolo vai junto.",
    "cfg.section.test": "Teste",
    "cfg.meter.mic": "Microfone",
    "cfg.meter.sys": "Áudio do sistema",
    "cfg.test": "Testar por 5 s",
    "cfg.test.running": "Fale e toque alguma coisa (um vídeo, uma chamada) na saída...",
    "cfg.test.busy": "Pare a gravação antes de testar os dispositivos.",
    "cfg.test.line": "{channel}: '{device}' - primeiro áudio em {first}, pico de {peak} dBFS",
    "cfg.test.error": "{channel}: {error}",
    "cfg.test.hint": "As duas barras devem se mexer. Se a barra do microfone também se mexe quando só a saída toca, você está na caixa de som: use fone, senão a voz dos outros vira 'Eu'.",
    "cfg.section.ending": "Quando a reunião termina",
    "cfg.default_preset": "Tipo de resumo padrão",
    "cfg.default_preset.hint": "Usado pelo 'Iniciar gravação' da bandeja e pelo atalho.",
    "cfg.on_stop": "Ao parar",
    "cfg.on_stop.ask": "Perguntar tipo, título e participantes",
    "cfg.on_stop.auto": "Resumir direto com o tipo padrão",
    "cfg.on_stop.hint": "A janela abre enquanto a transcrição termina, então não acrescenta espera.",
    "cfg.transcript_block": "Formato da transcrição",
    "cfg.transcript_block.callout": "Callout do Obsidian (recolhível)",
    "cfg.transcript_block.details": "<details> do HTML (VS Code, GitHub)",
    "cfg.transcript_block.hint": "Como a transcrição aparece recolhida no fim da nota.",
    "cfg.section.save_audio": "Áudio da reunião",
    "cfg.save_audio": "Guardar o áudio da reunião (um FLAC por canal)",
    "cfg.save_audio.hint": "Desligado por padrão: o áudio só existe no disco até ser transcrito. Ligado, ele fica guardado - e tem a voz de outras pessoas.",
    "cfg.audio_dir": "Pasta do áudio",
    "cfg.audio_dir.hint": "Vazio = uma pasta 'audio' dentro da pasta das notas.",
    "cfg.section.limits": "Travas de segurança",
    "cfg.silence_alert": "Alerta de silêncio (min)",
    "cfg.silence_alert.hint": "Avisa quando ninguém fala há esse tempo. 0 desliga.",
    "cfg.max_hours": "Parar depois de (h)",
    "cfg.max_hours.hint": "Para sozinho e escreve a nota, sem perguntar. 0 desliga.",
    "cfg.preset.new": "Novo",
    "cfg.preset.remove": "Remover",
    "cfg.preset.new_name": "Novo tipo {n}",
    "cfg.preset.new_prompt": "Reunião de trabalho. Escreva o corpo com estas seções:\n## Resumo\n## Próximos passos",
    "cfg.preset.name": "Nome",
    "cfg.preset.description": "Descrição",
    "cfg.preset.prompt": "Foco e seções",
    "cfg.preset.prompt.hint": "O que esse tipo de reunião precisa e quais seções '##' o resumo tem. Idioma, formato e 'não invente nada' entram sozinhos.",
    "cfg.preset.keywords": "Termos",
    "cfg.preset.keywords.hint": "Um por linha. Somados aos termos globais nas reuniões deste tipo.",
    "cfg.preset.languages": "Idiomas da fala",
    "cfg.preset.languages.hint": "Vazio usa os da aba Geral.",
    "cfg.preset.model": "Modelo do resumo",
    "cfg.preset.model.hint": "Vazio usa o modelo de resumo global.",
    "dlg.keep_one_preset": "Precisa sobrar pelo menos um tipo de resumo.",
    "dlg.remove_preset": "Remover o tipo de resumo '{name}'?",
    "dlg.preset_invalid": "Tipo de resumo inválido: {error}",
    "dlg.hotkey_same": "Os dois atalhos são iguais.",
    "tray.tooltip": "Apolo",
    "tray.toggle.start": "Iniciar gravação ({name})",
    "tray.toggle.stop": "Parar gravação",
    "tray.toggle.wait": "Aguarde...",
    "tray.open_last_note": "Abrir última nota",
    "tray.start_with": "Iniciar com o tipo de resumo",
    "tray.open_notes": "Abrir pasta das notas",
    "tray.settings": "Configurações...",
    "tray.quit": "Sair",
    "tray.notify.no_key": "Configure sua API key da OpenAI para começar.",
    "tray.notify.no_tray": "Esta sessão não tem área de notificação, então o ícone não tem onde aparecer. Abra o Apolo pelo menu de aplicativos para chegar nas configurações.",
    "err.no_api_key": "API key da OpenAI não configurada.",
    "err.no_api_key.detail": "Abra as configurações do Apolo pelo ícone na bandeja.",
    "err.no_api_key.cli": "Abra as configurações do Apolo ou defina OPENAI_API_KEY.",
    "err.invalid_api_key": "API key da OpenAI inválida ou revogada.",
    "err.invalid_api_key.detail": "Confira a chave nas configurações do Apolo.",
    "err.no_model_access": "Sua conta não tem acesso a este modelo.",
    "err.rate_limit": "Limite de uso da OpenAI atingido. Tentando de novo em instantes.",
    "err.timeout": "A OpenAI demorou demais para responder.",
    "err.offline": "Sem conexão com a OpenAI.",
    "err.offline.detail": "O áudio pendente espera no disco até a conexão voltar.",
    "err.api_status": "A OpenAI retornou erro {status}.",
    "err.unexpected": "Falha inesperada.",
    "err.hotkey_invalid": "Atalho inválido: '{spec}'.",
    "err.hotkey_invalid.detail": "Use o formato do pynput, ex: <ctrl>+<alt>+m.",
    "err.hotkey_empty": "Atalho vazio para '{mode}'.",
    "err.hotkey_conflict": "O atalho '{spec}' está registrado duas vezes ('{first}' e '{second}').",
    "err.hotkey_problems": "Atalhos com problema",
    "err.config_read": "Não consegui ler {file}. Corrija ou apague o arquivo.",
    "err.config_object": "config.json deveria conter um objeto JSON.",
    "err.presets_list": "presets.json deveria conter uma lista não vazia.",
    "err.preset_invalid": "Preset inválido em presets.json.",
    "err.keyring_save": "Não consegui salvar a API key no Gerenciador de Credenciais.",
    "err.startup_write": "Não consegui alterar a inicialização com o sistema.",
    "err.notes_dir": "Não consegui criar a pasta das notas.",
    "err.note_write": "Não consegui escrever a nota.",
    "err.note_open": "Não consegui abrir a nota.",
    "err.no_audio": "Não chegou áudio nem do microfone nem do sistema.",
    "err.no_presets": "Nenhum tipo de resumo configurado.",
    "cfg.title": "Apolo - Configurações",
    "cfg.tab.general": "  Geral  ",
    "cfg.save": "Salvar",
    "cfg.cancel": "Cancelar",
    "cfg.browse": "Escolher...",
    "cfg.section.openai": "OpenAI",
    "cfg.section.transcription": "Transcrição",
    "cfg.section.notes": "Notas",
    "cfg.section.interface": "Interface",
    "cfg.section.behavior": "Comportamento",
    "cfg.section.files": "Arquivos de configuração",
    "cfg.api_key": "API key",
    "cfg.api_key.hint": "Guardada no Gerenciador de Credenciais. Atual: {current}. Deixe em branco para manter.",
    "cfg.api_key.hint_artemis": "Guardada no Gerenciador de Credenciais. Usando a chave do Artemis Dictation ({current}), porque o Apolo ainda não tem uma própria. Preencha para usar outra.",
    "cfg.api_key.hint_env": "Usando OPENAI_API_KEY do ambiente ({current}). Preencha para guardar uma chave no Gerenciador de Credenciais.",
    "cfg.api_key.none": "(nenhuma)",
    "cfg.stt_model": "Transcrição",
    "cfg.stt_model.hint": "O gpt-transcribe aceita vários idiomas e uma lista de termos.",
    "cfg.text_model": "Resumo",
    "cfg.text_model.hint": "Modelo que escreve o resumo quando a reunião termina. Cada tipo de resumo pode usar outro.",
    "cfg.languages": "Idiomas da fala",
    "cfg.languages.hint": "Códigos de duas letras separados por vírgula. 'pt, en' cobre português com termos em inglês no meio. Vazio deixa o modelo detectar.",
    "cfg.keywords": "Termos",
    "cfg.keywords.hint": "Um por linha. Nomes que a transcrição costuma errar: pessoas, clientes, produtos, jargão.",
    "cfg.my_name": "Seu nome",
    "cfg.my_name.hint": "Quem é o 'Eu' da transcrição, para as tarefas saírem com o seu nome.",
    "cfg.notes_dir": "Pasta das notas",
    "cfg.notes_dir.hint": "Onde as notas das reuniões (.md) são escritas. Pode ser uma pasta do seu vault do Obsidian.",
    "cfg.ui_language": "Idioma da interface",
    "cfg.ui_language.hint": "Muda menus, configurações e mensagens. 'Automático' segue o sistema.",
    "cfg.overlay": "Indicador na tela",
    "cfg.overlay.always": "Sempre mostrar",
    "cfg.overlay.errors": "Só em erros",
    "cfg.overlay.never": "Nunca mostrar",
    "cfg.overlay.hint": "O indicador flutuante no canto inferior direito, com o tempo de gravação. O ícone da bandeja muda de cor de qualquer forma.",
    "cfg.autostart": "Iniciar o Apolo junto com o sistema",
    "cfg.autostart.hint": "Cria uma entrada só para o seu usuário - não precisa de administrador.",
    "cfg.autostart.frozen_hint": "Comando registrado: {command}",
    "cfg.open_folder": "Abrir pasta",
    "cfg.reload": "Recarregar do disco",
    "cfg.files.hint": "Use 'Recarregar' depois de editar config.json ou presets.json na mão. O log fica em apolo.log, na mesma pasta.",
    "dlg.save_failed": "Não consegui salvar: {error}",
    "app.tagline": "Notas de reunião locais",
    "desktop.settings": "Configurações",
    "desktop.keywords": "reunião;notas;transcrição;resumo;meeting;",
    "preset.geral.name": "Geral",
    "preset.geral.description": "Qualquer reunião: resumo, decisões, próximos passos.",
    "preset.geral.system_prompt": (
        "Reunião de trabalho geral. Escreva o corpo com estas seções, nesta "
        "ordem:\n"
        "## Resumo - de 3 a 6 frases: do que se tratou e onde a conversa "
        "chegou.\n"
        "## Decisões - o que foi decidido, um item por decisão.\n"
        "## Próximos passos - lista de tarefas no formato '- [ ] "
        "**Responsável**: tarefa (prazo, se citado)'.\n"
        "## Pontos em aberto - o que foi levantado e ficou sem resposta.\n"
        "Omita a seção inteira se não houver nada para ela."
    ),
    "preset.daily.name": "Daily",
    "preset.daily.description": "Daily: quem fez o quê, planos e impedimentos.",
    "preset.daily.system_prompt": (
        "Daily de um time de software. Escreva o corpo com estas seções, "
        "nesta ordem:\n"
        "## Resumo - de 1 a 3 frases sobre o estado geral.\n"
        "## Por pessoa - um subitem por pessoa com: fez / vai fazer / "
        "impedimentos. Use os nomes que der para identificar na conversa; o "
        "que não der para atribuir, agrupe em 'Outros'.\n"
        "## Impedimentos - todos os bloqueios, com quem pode desbloquear.\n"
        "## Próximos passos - lista de tarefas no formato '- [ ] "
        "**Responsável**: tarefa'.\n"
        "Seja breve: nota de daily é lida em 30 segundos."
    ),
    "preset.cliente.name": "Cliente",
    "preset.cliente.description": "Reunião com cliente: necessidades, acordos, compromissos.",
    "preset.cliente.system_prompt": (
        "Reunião com um cliente ou parceiro externo. Escreva o corpo com estas "
        "seções, nesta ordem:\n"
        "## Contexto - quem participou e o objetivo da reunião.\n"
        "## Necessidades e dores do cliente - com as palavras do cliente "
        "sempre que possível.\n"
        "## O que foi acordado - inclusive escopo, preço e datas, se "
        "citados.\n"
        "## Compromissos - duas listas: 'Nossos' e 'Do cliente'.\n"
        "## Riscos - riscos e pontos de atenção que você percebeu.\n"
        "## Próximos passos - lista de tarefas no formato '- [ ] "
        "**Responsável**: tarefa (prazo)'.\n"
        "## Rascunho de e-mail de follow-up - um e-mail curto e cordial "
        "recapitulando os acordos e os próximos passos, pronto para enviar.\n"
        "Omita a seção inteira se não houver nada para ela."
    ),
    "preset.1on1.name": "1:1",
    "preset.1on1.description": "Conversa individual: temas, feedbacks e combinados.",
    "preset.1on1.system_prompt": (
        "Reunião 1:1 entre duas pessoas (gestor e liderado, ou pares). "
        "Escreva o corpo com estas seções, nesta ordem:\n"
        "## Temas - o que foi conversado, um item por tema.\n"
        "## Feedbacks - feedbacks dados e recebidos, nos dois sentidos.\n"
        "## Combinados - lista de tarefas no formato '- [ ] "
        "**Responsável**: tarefa'.\n"
        "## Para a próxima 1:1 - o que acompanhar.\n"
        "Mantenha um tom respeitoso e neutro; não julgue as pessoas."
    ),
    # -- audio (fase 2)
    "err.vad_load": "Não consegui carregar o detector de voz (Silero VAD).",
    "err.mic_list": "Não consegui listar os microfones.",
    "err.mic_open": "Não consegui abrir o microfone.",
    "err.mic_busy": "O microfone está ocupado ou indisponível.",
    "err.mic_lost": "O microfone parou de entregar áudio.",
    "err.loopback_list": "Não consegui listar as saídas de áudio.",
    "err.loopback_open": "Não consegui capturar o áudio do sistema (loopback).",
    "err.loopback_lost": "A captura do áudio do sistema (loopback) parou.",
    "err.audio_unexpected": "Falha inesperada na captura de áudio.",
    "err.spool_write": "Não consegui gravar o áudio temporário no disco.",
    "err.spool_delete": "Não consegui apagar o áudio temporário.",
    "err.spool_missing": "Sessão não encontrada no spool de áudio.",
    "err.wav_read": "Não consegui ler o arquivo WAV.",
    "err.wav_format": "WAV não suportado: use PCM de 16 bits.",
    "cli.default": "padrão",
    "cli.devices.inputs": "Microfones (mic_device, canal \"Eu\"):",
    "cli.devices.outputs": "Saídas cujo loopback vira \"Outros\" (loopback_device):",
    "cli.devices.legend": "* = padrão do sistema",
    "cli.devices.configured": "Configurado: mic = {mic} | loopback = {loopback}",
    "cli.capture.preparing": "Preparando (o mic Bluetooth pode levar ~1,5 s)...",
    "cli.capture.no_first_block": "Nenhum áudio de {channel} depois de {seconds:.0f}s.",
    "cli.capture.recording": "Gravando por {seconds:.0f}s. Fale e toque algo na saída. Ctrl+C para.",
    "cli.capture.channel_error": "{channel} parou: {error}",
    "cli.capture.channel_stats": "{channel}: '{device}' | 1º bloco após {first} | fala {speech:.1f}s | ajustes de relógio {resyncs}",
    "cli.capture.spool_kept": "Trechos guardados para ouvir em {path}",
    "cli.capture.spool_deleted": "Áudio temporário apagado (use --keep-spool para guardar).",
    "cli.chunks.header": "canal    início   duração    fala   nível",
    "cli.chunks.summary": "{count} trechos, {sent:.1f}s de {total:.1f}s de áudio ({pct:.0f}%)",
    "cli.chunks.none": "Nenhum trecho: nenhuma fala detectada.",
    "cli.from_wav.file": "{name}: {seconds:.1f}s",
    "cli.simulate.note": "Nota ao vivo: {path}",
    "cli.simulate.done": "Pronto. Nota: {path}",
    "err.quota": "Sua conta da OpenAI está sem crédito.",
    "err.quota.detail": "Confira o billing em platform.openai.com. O áudio pendente fica no disco.",
    "err.transcription_failed": "Falha na transcrição.",
    "err.summary_failed": "Não consegui gerar o resumo.",
    "err.summary_refused": "O modelo se recusou a resumir esta reunião.",
    "err.summary_empty": "O modelo devolveu um resumo vazio.",
    "speaker.me": "Eu",
    "speaker.others": "Outros",
    "note.recording_title": "Gravando",
    "note.untitled": "Reunião",
    "note.tag": "reuniao",
    "note.header.date_fmt": "%d/%m/%Y %H:%M",
    "note.header.recording": "gravando",
    "note.header.with": "com {names}",
    "note.summary": "Resumo",
    "note.summary.pending": "Gerando ao parar.",
    "note.my_notes": "Minhas anotações",
    "note.my_notes.placeholder": "(o que você escrever aqui durante a reunião é preservado e usado no resumo)",
    "note.transcript": "Transcrição",
    "note.transcript.live": "Transcrição (ao vivo)",
    "note.transcript.full": "Transcrição completa ({duration})",
    "note.transcript.empty": "nada transcrito ainda",
    "note.gap": "trecho não transcrito",
    "note.gap.reason.offline": "sem conexão",
    "note.gap.reason.overflow": "áudio pendente demais",
    "note.gap.reason.error": "falha na transcrição",
    "summary.rules": (
        "Você escreve notas de reunião a partir de uma transcrição "
        "automática.\n"
        "Regras:\n"
        "- Escreva no idioma predominante da reunião. Mantenha os termos "
        "técnicos em inglês exatamente como foram falados.\n"
        "- Não invente nada: use só o que está na transcrição e nas anotações "
        "do usuário. Se algo não ficou claro, diga que não ficou claro.\n"
        "- '{me}' é {my_name}, quem gravou a reunião. Use esse nome nas "
        "tarefas dele.\n"
        "- '{others}' é o áudio do computador e pode ser várias pessoas. "
        "Deduza os nomes pelo contexto (quem é chamado pelo nome, quem "
        "responde) e pela lista de participantes; quando não der, não atribua "
        "a ninguém.\n"
        "- As anotações do usuário mostram o que ele achou importante: dê "
        "peso a elas.\n"
        "- As linhas marcadas como '{gap}' são lacunas da gravação. Não tente "
        "preencher o que foi dito nelas.\n"
        "- A transcrição automática erra palavras; corrija pelo contexto só "
        "quando for óbvio."
    ),
    "summary.me_unknown": "a pessoa que gravou a reunião",
    "summary.format.json": (
        "Responda com um objeto JSON com:\n"
        "- title: título curto e específico da reunião (até ~60 caracteres), "
        "sem a data.\n"
        "- tags: de 1 a 5 tags em minúsculas, sem espaços (use hífen), sem "
        "'#'.\n"
        "- body_markdown: o corpo da nota em Markdown, com as seções pedidas "
        "acima, começando no primeiro '##'. Sem título e sem frontmatter."
    ),
    "summary.format.markdown": (
        "Responda só em Markdown. Na 1ª linha, o título da reunião (curto, "
        "sem '#' e sem a data). Depois uma linha em branco e o corpo, com as "
        "seções pedidas acima, começando no primeiro '##'."
    ),
    "summary.ctx.date": "Data: {date}",
    "summary.ctx.duration": "Duração: {minutes} min",
    "summary.ctx.participants": "Participantes: {names}",
    "summary.ctx.me": "'{me}' na transcrição: {name}",
    "summary.ctx.notes": "Anotações do usuário",
    "summary.ctx.no_notes": "(nenhuma)",
    "summary.ctx.transcript": "Transcrição",
}
