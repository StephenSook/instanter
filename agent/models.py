"""Structured-output contracts for the model's two legitimate jobs.

Perceive: read free-text intake notes into typed observations a human can
verify (and the deterministic ladder can consume). Communicate: explain an
escalation in operative facts. Neither output may contain legal advice, and
the boundary is enforced HERE, in validation, not in prompt hope: a
generation that drifts into advice fails validation and is retried or
routed to a human, never delivered.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field, field_validator, model_validator

# Below this confidence an extraction is not trustworthy enough to stand
# without a human: the validator forces needs_human_confirmation, and the
# triage policy independently floors on it (defense in depth).
LOW_CONFIDENCE_THRESHOLD = 0.6

# Advice-language boundary: the hard RUNTIME FLOOR, not the guarantee. No
# blocklist is complete; the guarantee in this system is architectural (a
# licensed attorney reviews every surfaced output, and the packet's draft
# answer keeps every defense field blank). This floor exists so obvious
# drift fails validation loudly and is audited, and it is layered: literal
# phrases, second-person directives, imperative legal-action sentence
# openings, and defense-coupling patterns, all matched over NFKC-normalized
# casefolded text so Unicode lookalikes and casing do not slip past.
_ADVICE_PHRASES: tuple[str, ...] = (
    "you should",
    "we recommend",
    "i recommend",
    "best defense",
    "strongest defense",
    "raise the defense",
    "assert the defense",
    "file a counterclaim",
    "you have a strong case",
    "legal advice",
    "advise the tenant",
    "the tenant should",
    "the tenant must",
    "the tenant needs to",
)

# Second-person directives: telling anyone what they legally should do.
_SECOND_PERSON_DIRECTIVE = re.compile(
    r"\byou\s+(should|must|need\s+to|have\s+to|ought\s+to|will\s+want\s+to|can\s+win|will\s+win)\b"
)
# Imperative sentence openings with a legal-action verb ("File an answer
# today..."). Sentence-initial only: factual prose about filings passes.
_LEGAL_ACTION_VERBS = (
    "file",
    "pay",
    "sue",
    "appeal",
    "contest",
    "plead",
    "appear",
    "claim",
    "assert",
    "vacate",
    "withhold",
    "countersue",
    "dispute",
)
_SENTENCE_SPLIT = re.compile(r"[.!?;\n]+")
# A legal-action verb coupled to a defense-shaped object anywhere in a
# sentence ("... and claim defective service").
_DEFENSE_COUPLING = re.compile(
    r"\b(claim|assert|raise|plead|argue)\b[^.!?;\n]{0,60}"
    r"\b(defense|defective|tender|counterclaim|estoppel|retaliation|habitability|jury)\b"
)

# NFKC folds compatibility forms (fullwidth, ligatures) but deliberately
# never folds Cyrillic or Greek letters into Latin, which is exactly the
# gap a lookalike payload walks through (a "You should ..." whose o is the
# Cyrillic letter). Common homoglyphs are folded to their Latin lookalikes
# BEFORE any matching (keys are escape sequences so the source itself
# carries no ambiguous glyphs); anything rarer trips the mixed-script
# check below.
_HOMOGLYPH_FOLD = str.maketrans(
    {
        # Cyrillic lowercase (matching runs on casefolded text)
        "\u0430": "a",  # CYRILLIC SMALL LETTER A
        "\u0435": "e",  # CYRILLIC SMALL LETTER IE
        "\u043e": "o",  # CYRILLIC SMALL LETTER O
        "\u0440": "p",  # CYRILLIC SMALL LETTER ER
        "\u0441": "c",  # CYRILLIC SMALL LETTER ES
        "\u0443": "y",  # CYRILLIC SMALL LETTER U
        "\u0445": "x",  # CYRILLIC SMALL LETTER HA
        "\u0456": "i",  # CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I
        "\u0458": "j",  # CYRILLIC SMALL LETTER JE
        "\u0455": "s",  # CYRILLIC SMALL LETTER DZE
        "\u04bb": "h",  # CYRILLIC SMALL LETTER SHHA
        "\u0501": "d",  # CYRILLIC SMALL LETTER KOMI DE
        "\u051b": "q",  # CYRILLIC SMALL LETTER QA
        "\u051d": "w",  # CYRILLIC SMALL LETTER WE
        # Greek lowercase
        "\u03bf": "o",  # GREEK SMALL LETTER OMICRON
        "\u03b1": "a",  # GREEK SMALL LETTER ALPHA
        "\u03b5": "e",  # GREEK SMALL LETTER EPSILON
        "\u03b9": "i",  # GREEK SMALL LETTER IOTA
        "\u03ba": "k",  # GREEK SMALL LETTER KAPPA
        "\u03bd": "v",  # GREEK SMALL LETTER NU
        "\u03c1": "p",  # GREEK SMALL LETTER RHO
        "\u03c4": "t",  # GREEK SMALL LETTER TAU
        "\u03c5": "u",  # GREEK SMALL LETTER UPSILON
        "\u03c7": "x",  # GREEK SMALL LETTER CHI
    }
)

# Words that politely precede an imperative without changing it ("Please
# file...", "Kindly pay..."). Skipped when locating the operative first
# word of a sentence; matching is fail-closed, so over-skipping only makes
# the floor stricter.
_IMPERATIVE_PREFIX_WORDS = frozenset(
    {
        "please",
        "kindly",
        "just",
        "simply",
        "do",
        "now",
        "then",
        "also",
        "next",
        "first",
        "immediately",
        "urgently",
        "promptly",
        "today",
    }
)

_EDGE_PUNCTUATION = re.compile(r"^[^0-9a-z]+|[^0-9a-z]+$")

# Digit and symbol substitutions that spell a protected word past a literal
# match ("F1le an answer"). Applied to a diacritic-stripped shadow copy for
# matching only; the delivered text is unchanged.
_LEET_FOLD = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
    }
)


def _forbidden_character(text: str) -> str | None:
    """Control (Cc) and format (Cf) characters have no place in factual
    memo text and are exactly how invisible-character payloads smuggle a
    protected word past matching (a zero-width joiner inside 'file') or
    ambiguity into a hash input. Newline and tab stay legal."""
    for ch in text:
        if ch in ("\n", "\t"):
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            return ch
    return None


def _strip_marks(text: str) -> str:
    """Diacritic-stripped shadow for matching: NFD-decompose, drop the
    nonspacing marks, so a combining acute cannot split 'file'."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )


# Single alphanumerics separated by runs of ANY non-alphanumeric
# characters, arbitrary length, punctuation and whitespace mixed
# ("f.i.l.e", "f. i. l. e", "F.-1-l-e", "F....i....l....e"): collapse the
# run by dropping the separators. A finite separator cap is a bypass with
# one more dot, so the length is unbounded; every single character in the
# run must be standalone (not a word prefix), so factual prose like
# "0 day(s) away; rank 1 of 2" is never absorbed. Matching shadow only.
_SEPARATED_RUN = re.compile(
    r"(?<![0-9a-z])[0-9a-z](?![0-9a-z])(?:[^0-9a-z]+[0-9a-z](?![0-9a-z])){2,}"
)


def _collapse_separated_letters(text: str) -> str:
    return _SEPARATED_RUN.sub(lambda m: re.sub(r"[^0-9a-z]", "", m.group(0)), text)


def _strip_intra_token_punctuation(text: str) -> str:
    """Remove punctuation INSIDE whitespace-delimited tokens ('n-ine' ->
    'nine', 'f-ile' -> 'file') so a separator between multi-character
    fragments cannot split a protected word. Token boundaries survive, so
    'pay-or-quit' becomes 'payorquit', never 'pay'. Matching shadow only."""
    return " ".join(re.sub(r"[^0-9a-z]+", "", token) for token in text.split())


def _collapse_spaced_letters(text: str) -> str:
    """Collapse runs of two-plus single-letter tokens ('F i l e') into one
    word so spacing cannot hide an imperative. Matching shadow only."""
    out: list[str] = []
    run: list[str] = []
    for token in text.split():
        if len(token) == 1 and token.isalpha():
            run.append(token)
            continue
        if len(run) >= 2:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        out.append(token)
    if len(run) >= 2:
        out.append("".join(run))
    else:
        out.extend(run)
    return " ".join(out)


def _mixed_script_token(text: str) -> str | None:
    """A token mixing Latin letters with another script is a lookalike
    payload this floor cannot read reliably; fail closed on it. Whole-token
    non-Latin text (a name, a quoted phrase) does not trip this."""
    for token in text.split():
        scripts = set()
        for ch in token:
            if ch.isalpha():
                scripts.add(unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0])
        if "LATIN" in scripts and len(scripts) > 1:
            return token
    return None


def _scan_variant(variant: str, field_name: str) -> None:
    for phrase in _ADVICE_PHRASES:
        if phrase in variant:
            raise ValueError(
                f"{field_name} contains advice language ({phrase!r}); this "
                "system states facts for a licensed attorney and never advises"
            )
    if _SECOND_PERSON_DIRECTIVE.search(variant):
        raise ValueError(
            f"{field_name} contains a second-person legal directive; this "
            "system states facts for a licensed attorney and never advises"
        )
    if _DEFENSE_COUPLING.search(variant):
        raise ValueError(
            f"{field_name} couples a legal action to a defense; this system "
            "states facts for a licensed attorney and never advises"
        )
    for sentence in _SENTENCE_SPLIT.split(variant):
        # The operative first word: strip bullet/bracket punctuation from
        # token edges (interior hyphens survive, so "pay-or-quit notice"
        # stays factual) and skip polite prefixes ("Please file...").
        words = [w for w in (_EDGE_PUNCTUATION.sub("", t) for t in sentence.split()) if w]
        index = 0
        while index < len(words) and words[index] in _IMPERATIVE_PREFIX_WORDS:
            index += 1
        if index < len(words) and words[index] in _LEGAL_ACTION_VERBS:
            raise ValueError(
                f"{field_name} opens a sentence with the imperative "
                f"{words[index]!r}; this system states facts for a licensed "
                "attorney and never advises"
            )


def _reject_advice_language(text: str, field_name: str) -> str:
    forbidden = _forbidden_character(text)
    if forbidden is not None:
        raise ValueError(
            f"{field_name} contains the control or format character "
            f"U+{ord(forbidden):04X}; factual memo text has no use for "
            "invisible characters and this floor fails closed on them"
        )
    normalized = unicodedata.normalize("NFKC", text).casefold().translate(_HOMOGLYPH_FOLD)
    mixed = _mixed_script_token(normalized)
    if mixed is not None:
        raise ValueError(
            f"{field_name} contains the mixed-script lookalike token "
            f"{mixed!r}; this floor cannot read it reliably and fails closed"
        )
    # Every check runs over each matching shadow: the text as normalized,
    # diacritic-stripped, leet-folded, space-collapsed, AND the canonical
    # composition of all of them (round 9: 'F 1 l e' evaded the independent
    # shadows because spacing and leet each individually resolved; the
    # composed shadow closes combined evasions). Delivered text is always
    # the original.
    demarked = _strip_marks(normalized)
    canonical = _collapse_spaced_letters(
        _collapse_separated_letters(demarked.translate(_LEET_FOLD))
    )
    for variant in {
        normalized,
        demarked,
        demarked.translate(_LEET_FOLD),
        _collapse_spaced_letters(demarked),
        _collapse_separated_letters(demarked),
        _strip_intra_token_punctuation(demarked),
        _strip_intra_token_punctuation(demarked.translate(_LEET_FOLD)),
        canonical,
    }:
        _scan_variant(variant, field_name)
    return text


# Model-authored packet text may carry no quantities at all: digits are the
# obvious channel, but "nine hundred ninety nine days" and "December thirty
# first" fabricate figures just as effectively, so number words and month
# names are banned wherever the deterministic fact sheet is the only
# legitimate source of numbers.
_NUMBER_WORDS = frozenset(
    [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
        "million",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
        "twentieth",
        "thirtieth",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
)
_WORD_TOKEN = re.compile(r"[a-z]+")


def reject_model_numerics(text: str, field_name: str) -> str:
    """Fail closed on any model-authored quantity: digits, number words,
    month names, over the SAME canonical shadows the advice floor scans
    (a combining mark or separated letters must not smuggle 'nïne' or
    'n.i.n.e' past a literal wordlist). The stated recovery matters: a
    live model retries on the error text, and a rejection with no way out
    starves the sweep into the floor."""
    if any(ch.isdigit() for ch in text):
        raise ValueError(
            f"{field_name} must contain no digits; every date, day count, "
            "and rank is rendered by the system. State the fact without "
            "the figure and resubmit."
        )
    normalized = unicodedata.normalize("NFKC", text).casefold().translate(_HOMOGLYPH_FOLD)
    demarked = _strip_marks(normalized)
    canonical = _collapse_spaced_letters(_collapse_separated_letters(demarked))
    for variant in {
        normalized,
        demarked,
        _collapse_separated_letters(demarked),
        _strip_intra_token_punctuation(demarked),
        canonical,
    }:
        for token in _WORD_TOKEN.findall(variant):
            if token in _NUMBER_WORDS:
                raise ValueError(
                    f"{field_name} contains the number or date word {token!r}; "
                    "quantities and dates come only from the system's fact "
                    "sheet. State the fact without the figure and resubmit."
                )
    return text


class ExtractedObservations(BaseModel):
    """Typed observations pulled from free-text intake notes.

    Observations are inputs to the deterministic ladder and statements for
    a human reviewer; they are never legal conclusions. Anything the model
    is unsure about belongs in ``ambiguities`` with
    ``needs_human_confirmation=True``, never guessed.
    """

    summary: str = Field(min_length=1, max_length=600)
    mentions_service_by_posting: bool | None = Field(
        default=None,
        description="Notes suggest tack-and-mail/posted service. None = not mentioned.",
    )
    mentions_answer_already_filed: bool | None = None
    mentions_hearing_or_deadline_change: bool | None = None
    mentions_possible_defective_service: bool | None = None
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Material open questions a staff member must confirm.",
        max_length=10,
    )
    needs_human_confirmation: bool = Field(
        description="True whenever any extracted signal is uncertain or conflicting."
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("summary")
    @classmethod
    def _summary_is_not_advice(cls, value: str) -> str:
        return _reject_advice_language(value, "summary")

    @field_validator("ambiguities")
    @classmethod
    def _ambiguities_are_not_advice(cls, value: list[str]) -> list[str]:
        for item in value:
            # Ambiguities are copied verbatim into the attorney packet's
            # fact sheet, so they get the full packet-text discipline: no
            # advice, no quantities (a question like "could the deadline
            # instead be <date>?" would plant a fabricated figure inside
            # the deterministic facts), and a bounded length so no entry
            # can crowd later ones out of the packet.
            if len(item) > 160:
                raise ValueError(
                    "each ambiguity must be 160 characters or fewer; state "
                    "one open question per entry"
                )
            _reject_advice_language(item, "ambiguities")
            reject_model_numerics(item, "ambiguities")
        return value

    @model_validator(mode="after")
    def _uncertainty_requires_confirmation(self) -> ExtractedObservations:
        # Cross-field invariant: an extraction cannot simultaneously declare
        # open questions (or low confidence) AND claim no human is needed.
        # That contradiction is exactly the shape that bypasses a fail-closed
        # floor while remaining schema-valid field by field.
        if self.ambiguities and not self.needs_human_confirmation:
            raise ValueError(
                "ambiguities are listed but needs_human_confirmation is "
                "false; open questions require needs_human_confirmation=true"
            )
        if self.confidence < LOW_CONFIDENCE_THRESHOLD and not self.needs_human_confirmation:
            raise ValueError(
                f"confidence {self.confidence} is below "
                f"{LOW_CONFIDENCE_THRESHOLD}; a low-confidence extraction "
                "requires needs_human_confirmation=true"
            )
        return self


class EffortEstimate(BaseModel):
    """Operational estimate of attorney minutes this case likely needs.

    Resource-axis context only: the deterministic ladder never lets this
    change a disposition level (bounded, and the threshold sits downstream).
    """

    minutes: int = Field(ge=5, le=240)
    basis: str = Field(min_length=1, max_length=300)

    @field_validator("basis")
    @classmethod
    def _basis_is_not_advice(cls, value: str) -> str:
        return _reject_advice_language(value, "basis")


class EscalationRationale(BaseModel):
    """The communicate-layer output: why THIS case, and not the others.

    The disposition itself is decided by the deterministic ladder; the
    model explains it. ``disposition`` must echo the ladder's decision
    (verified by the caller against the TriageDecision), and
    ``contributing_factors`` must restate the deterministic factors, so the
    explanation is faithful rather than decorative.
    """

    case_id: str = Field(min_length=1, max_length=64)
    disposition: str = Field(description="Echo of the ladder's level value, e.g. 'interrupt'.")
    contributing_factors: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=900)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("rationale")
    @classmethod
    def _rationale_is_not_advice(cls, value: str) -> str:
        return _reject_advice_language(value, "rationale")

    @field_validator("contributing_factors")
    @classmethod
    def _factors_are_not_advice(cls, value: list[str]) -> list[str]:
        for item in value:
            _reject_advice_language(item, "contributing_factors")
        return value

    @field_validator("rationale")
    @classmethod
    def _rationale_carries_facts_not_scores(cls, value: str) -> str:
        # A bare urgency number in place of facts is the research's fourth
        # collapse condition. Cheap structural check: forbid "urgency" being
        # presented as a numeric score.
        lowered = value.lower()
        if "urgency score" in lowered or "urgency: 0." in lowered:
            raise ValueError("rationale must state operative facts, never a bare urgency score")
        return value
