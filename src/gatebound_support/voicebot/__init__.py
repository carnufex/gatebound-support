"""Discord voice bridge: a player joins a voice channel, runs /support (or /call), and the
bot bridges audio both ways to the ElevenLabs Conversational AI agent. Runs as its own
long-lived process (``gatebound-support voicebot``) — see runner.py — never inside the
FastAPI app (app.py).
"""
