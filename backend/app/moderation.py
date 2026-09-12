"""
Public-profile-text moderation (nickname, bio). No moderation utility
or dependency existed anywhere in this codebase before this pass.

Uses `better-profanity` (see requirements.txt) rather than a hand-rolled
word list — it ships a real, maintained wordlist (hundreds of entries:
slurs, hate speech, harassment terms, severe profanity) and already
does some normalization internally. On top of that, this module adds:

  - Unicode NFKC normalization (catches lookalike/fullwidth characters)
  - case folding
  - collapsing runs of 3+ repeated characters ("fuuuuuck" -> "fuck")
  - for short single-token strings only (nicknames) — also stripping
    common separator characters used to break a word up ("f.u.c.k",
    "f u c k") before checking again

That last step is deliberately NOT applied to free-text fields like a
bio: collapsing all whitespace in a multi-sentence bio would merge
unrelated words together and risks flagging ordinary text that happens
to form a bad substring across a word boundary. A nickname is normally
one short token, so the risk of that particular false positive doesn't
apply there, while it's exactly where a "put spaces in it" bypass
attempt shows up.

This is intentionally not trying to be a perfect, unbeatable filter —
matching the spec's own ask ("obvious variations where reasonably
practical"), not building a research-grade classifier.
"""
import re
import unicodedata

from better_profanity import profanity

profanity.load_censor_words()

_REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")
_SEPARATOR_RE = re.compile(r"[\s\-_.*+]+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = _REPEATED_CHAR_RE.sub(r"\1", text)
    return text


def is_inappropriate(text: str | None, *, despace: bool = False) -> bool:
    """
    `despace=True` additionally checks the text with separator
    characters stripped out entirely — appropriate for a single-token
    field like a nickname, not for multi-word free text (see module
    docstring for why).
    """
    if not text:
        return False
    normalized = _normalize(text)
    if profanity.contains_profanity(normalized):
        return True
    if despace:
        collapsed = _SEPARATOR_RE.sub("", normalized)
        if collapsed != normalized and profanity.contains_profanity(collapsed):
            return True
    return False
