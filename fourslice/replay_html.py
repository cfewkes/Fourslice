"""
fourslice/replay_html.py

Generates a self-contained HTML file from a Showdown-protocol battle
log, structured to match a real saved Showdown replay page -- the
same replay-embed.js script Showdown hosts renders it into the full
interactive player. Works identically for a real replay's log or a
synthetic one, once footage-to-log conversion exists.

See top of this file in the project for full confidence-level notes
on what's confirmed (structure) vs. inferred (slash-escaping, the
omitted narration block) vs. completely unverified (actual browser
rendering -- never tested, no display or network access to
Showdown from this environment).
"""

import html as html_module
import string

ESCAPE_SLASHES = True

_CSS = """html,body {font-family:Verdana, sans-serif;font-size:10pt;margin:0;padding:0;}body{padding:12px 0;} .battle-log {font-family:Verdana, sans-serif;font-size:10pt;} .battle-log-inline {border:1px solid #AAAAAA;background:#EEF2F5;color:black;max-width:640px;margin:0 auto 80px;padding-bottom:5px;} .battle-log .inner {padding:4px 8px 0px 8px;} .battle-log .inner-preempt {padding:0 8px 4px 8px;} .battle-log .inner-after {margin-top:0.5em;} .battle-log h2 {margin:0.5em -8px;padding:4px 8px;border:1px solid #AAAAAA;background:#E0E7EA;border-left:0;border-right:0;font-family:Verdana, sans-serif;font-size:13pt;} .battle-log .chat {vertical-align:middle;padding:3px 0 3px 0;font-size:8pt;} .battle-log .chat strong {color:#40576A;} .battle-log .chat em {padding:1px 4px 1px 3px;color:#000000;font-style:normal;} .chat.mine {background:rgba(0,0,0,0.05);margin-left:-8px;margin-right:-8px;padding-left:8px;padding-right:8px;} .spoiler {color:#BBBBBB;background:#BBBBBB;padding:0px 3px;} .spoiler:hover, .spoiler:active, .spoiler-shown {color:#000000;background:#E2E2E2;padding:0px 3px;} .spoiler a {color:#BBBBBB;} .spoiler:hover a, .spoiler:active a, .spoiler-shown a {color:#2288CC;} .chat code, .chat .spoiler:hover code, .chat .spoiler:active code, .chat .spoiler-shown code {border:1px solid #C0C0C0;background:#EEEEEE;color:black;padding:0 2px;} .chat .spoiler code {border:1px solid #CCCCCC;background:#CCCCCC;color:#CCCCCC;} .battle-log .rated {padding:3px 4px;} .battle-log .rated strong {color:white;background:#89A;padding:1px 4px;border-radius:4px;} .spacer {margin-top:0.5em;} .message-announce {background:#6688AA;color:white;padding:1px 4px 2px;} .message-announce a, .broadcast-green a, .broadcast-blue a, .broadcast-red a {color:#DDEEFF;} .broadcast-green {background-color:#559955;color:white;padding:2px 4px;} .broadcast-blue {background-color:#6688AA;color:white;padding:2px 4px;} .infobox {border:1px solid #6688AA;padding:2px 4px;} .infobox-limited {max-height:200px;overflow:auto;overflow-x:hidden;} .broadcast-red {background-color:#AA5544;color:white;padding:2px 4px;} .message-learn-canlearn {font-weight:bold;color:#228822;text-decoration:underline;} .message-learn-cannotlearn {font-weight:bold;color:#CC2222;text-decoration:underline;} .message-learn-list {margin-top:0;margin-bottom:0;} .message-throttle-notice, .message-error {color:#992222;} .message-overflow, .chat small.message-overflow {font-size:0pt;} .message-overflow::before {font-size:9pt;content:'...';} .subtle {color:#3A4A66;}"""

_TEMPLATE = string.Template("""<!DOCTYPE html>
<meta charset="utf-8" />
<title>$title</title>
<style>
$css
</style>
<div class="wrapper replay-wrapper" style="max-width:1180px;margin:0 auto">
<input type="hidden" name="replayid" value="$replay_id" />
<div class="battle"></div><div class="battle-log"></div><div class="replay-controls"></div><div class="replay-controls-2"></div>
<h1 style="font-weight:normal;text-align:center"><strong>$title</strong><br /><a href="https://pokemonshowdown.com/users/$p1_slug" class="subtle" target="_blank">$p1_name</a> vs. <a href="https://pokemonshowdown.com/users/$p2_slug" class="subtle" target="_blank">$p2_name</a></h1>
<script type="text/plain" class="battle-log-data">$log</script>
</div>
<script>
let daily = Math.floor(Date.now()/1000/60/60/24);document.write('<script src="https://play.pokemonshowdown.com/js/replay-embed.js?version'+daily+'"></'+'script>');
</script>
""")


def render_replay_html(log_text: str, title: str, p1_name: str, p2_name: str, replay_id: str = "local") -> str:
    safe_log = log_text.replace("/", "\\/") if ESCAPE_SLASHES else log_text

    return _TEMPLATE.substitute(
        title=html_module.escape(title),
        css=_CSS,
        replay_id=html_module.escape(str(replay_id)),
        p1_name=html_module.escape(p1_name),
        p2_name=html_module.escape(p2_name),
        p1_slug=html_module.escape(p1_name.lower()),
        p2_slug=html_module.escape(p2_name.lower()),
        log=safe_log,
    )