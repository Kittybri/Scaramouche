"""Interactive Wanderer-story tarot system shared by both Discord bots."""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import sqlite3
import secrets
import time
from functools import wraps
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass(frozen=True)
class TarotCard:
    stem: str
    name: str
    standard_upright: str
    standard_reversed: str
    lore_upright: str
    lore_reversed: str
    polarity: int = 0

    def meaning(self, reversed_card: bool, mode: str = "lore") -> str:
        if mode == "standard":
            return self.standard_reversed if reversed_card else self.standard_upright
        return self.lore_reversed if reversed_card else self.lore_upright


@dataclass(frozen=True)
class DrawnCard:
    card: TarotCard
    position: str
    reversed: bool

    @property
    def orientation(self) -> str:
        return "reversed" if self.reversed else "upright"

    def meaning(self, mode: str = "lore") -> str:
        return self.card.meaning(self.reversed, mode)


@dataclass(frozen=True)
class Spread:
    key: str
    label: str
    positions: tuple[str, ...]
    brief_words: int
    brief_tokens: int


@dataclass(frozen=True)
class TarotPreferences:
    reversals: bool = True
    visibility: str = "public"
    detail: str = "brief"
    meaning_mode: str = "lore"


@dataclass(frozen=True)
class YesNoAnalysis:
    label: str
    explanation: str
    score: float
    positive: int
    uncertain: int
    warning: int


# Every card has a standard interpretation and a separate meaning tied directly
# to the character, scene, and era shown in this custom deck.
CARD_ROWS = (
    ("major-00-the-fool", "The Fool", "new beginnings, innocence, a leap of faith", "recklessness, fear of beginning", "Kabukimono steps into human life with an open heart and no map", "innocence is being exploited, or fear keeps the first step frozen", 1),
    ("major-01-the-magician", "The Magician", "willpower, skill, focused creation", "manipulation, scattered power", "Ei's act of creation says the tools already exist; intention decides their purpose", "a creator or authority is using power without accepting responsibility", 1),
    ("major-02-the-high-priestess", "The High Priestess", "intuition, mystery, inner knowledge", "secrets, ignored intuition", "Yae Miko guards what is unspoken; observe before revealing your hand", "a protected secret has become avoidance, and intuition is being silenced", 0),
    ("major-03-the-empress", "The Empress", "care, abundance, creativity", "dependence, creative stagnation", "Ei's conflicted creation asks whether care can exist without possession", "neglect hides behind duty, or affection has become controlling", 1),
    ("major-04-the-emperor", "The Emperor", "structure, authority, stability", "control, rigidity, domination", "the Shogun puppet offers perfect structure at the cost of flexibility", "rigid control is protecting an empty throne rather than a living purpose", 0),
    ("major-05-the-hierophant", "The Hierophant", "tradition, teaching, belonging", "rebellion, restrictive convention", "Niwa's forge teaches that belonging is built through shared craft and trust", "tradition has stopped teaching and started demanding obedience", 0),
    ("major-06-the-lovers", "The Lovers", "alignment, devotion, meaningful choice", "disharmony, avoidance, divided values", "Kabukimono and Niwa choose trust; the decisive bond is the one freely accepted", "a bond is fractured because fear or divided values replaced honest choice", 1),
    ("major-07-the-chariot", "The Chariot", "resolve, direction, victory", "lost direction, aggression, delay", "the Balladeer advances through sheer will; choose a direction before momentum chooses it", "ambition is driving without a destination and mistaking force for control", 1),
    ("major-08-strength", "Strength", "courage, patience, gentle control", "self-doubt, raw impulse, depleted courage", "Nahida meeting Shouki no Kami shows that patient courage can restrain enormous force", "wounded pride is overpowering restraint, or compassion has become exhaustion", 1),
    ("major-09-the-hermit", "The Hermit", "reflection, solitude, inner guidance", "isolation, withdrawal, refusal of guidance", "beneath the Fatui veil, solitude can expose the truth no audience will say", "withdrawal has hardened into a prison where only old grievances answer", 0),
    ("major-10-wheel-of-fortune", "Wheel of Fortune", "change, cycles, fortunate movement", "resistance, setbacks, repeating cycles", "the three betrayals turn again; a repeated story can be interrupted by a new response", "the same wound keeps choosing the next scene because its lesson is refused", 1),
    ("major-11-justice", "Justice", "truth, accountability, fair consequence", "dishonesty, imbalance, evasion", "Niwa's true fate surfaces; accountability begins when the manufactured story is named", "Dottore's deception still shapes the verdict, or blame is being assigned for convenience", 0),
    ("major-12-the-hanged-man", "The Hanged Man", "surrender, pause, a changed perspective", "stalling, needless sacrifice, resistance", "tethered to the fallen mech, Scaramouche must release the Gnosis he cannot keep", "desperation clings to the lost source of worth and turns surrender into torment", 0),
    ("major-13-death", "Death", "ending, transformation, necessary release", "clinging, stagnation, fear of change", "the Balladeer dissolves in Irminsul so an identity can end without ending the self", "erasing a name is being mistaken for healing what the name carried", 0),
    ("major-14-temperance", "Temperance", "balance, healing, patient integration", "excess, friction, imbalance", "Nahida restores memory while Anemo and Electro coexist; healing means integration, not erasure", "past and present selves are fighting for control instead of becoming one history", 1),
    ("major-15-the-devil", "The Devil", "attachment, temptation, a binding pattern", "release, reclaiming power, seeing the chain", "Dottore's strings reveal the bargain that calls captivity power", "the strings are visible now; release begins by refusing the role they assigned", -1),
    ("major-16-the-tower", "The Tower", "upheaval, revelation, broken illusion", "avoided truth, prolonged collapse", "Shouki no Kami falls and false divinity shatters; the collapse exposes what was never stable", "the inevitable fall is being delayed while the damaged structure spreads", -1),
    ("major-17-the-star", "The Star", "hope, renewal, honest inspiration", "discouragement, lost faith", "the Anemo Vision arrives after truth is accepted; hope returns as chosen direction", "renewal is near, but shame keeps the sky covered", 1),
    ("major-18-the-moon", "The Moon", "uncertainty, dreams, hidden emotion", "clarity emerging, fear losing its hold", "Kabukimono, Balladeer, and Wanderer overlap beneath Irminsul's moon; every hidden self wants recognition", "the masks are separating and distorted memory is beginning to clear", 0),
    ("major-19-the-sun", "The Sun", "joy, success, vitality", "delayed joy, unrealistic optimism", "a difficult Sumeru dawn proves warmth can be real without pretending the night never happened", "joy is being distrusted, delayed, or forced into a performance", 1),
    ("major-20-judgement", "Judgement", "reckoning, awakening, second chances", "self-condemnation, refusal to learn", "Wanderer faces every former self and chooses accountability without self-erasure", "the past has become a courtroom with no sentence except endless punishment", 1),
    ("major-21-the-world", "The World", "completion, integration, earned wholeness", "unfinished business, incomplete closure", "Wanderer flies through a complete circle of memories; freedom comes from carrying the whole truth", "the journey looks finished, but one rejected self still waits outside the circle", 1),

    ("wands-ace", "Ace of Wands", "inspiration, potential, a creative spark", "delay, drained motivation, a false start", "the Anemo Vision ignites a self-chosen purpose that no creator assigned", "the new wind is present, but fear of wanting something smothers it", 1),
    ("wands-two", "Two of Wands", "planning, future decisions, personal power", "fear of change, poor planning, restricted options", "from the Sumeru tower, Wanderer sees several paths and must choose one deliberately", "the horizon is wide, yet old boundaries are being treated as permanent", 0),
    ("wands-three", "Three of Wands", "expansion, foresight, progress", "delays, obstacles, playing small", "leaving Sumeru turns reflection into exploration and planning into movement", "the wider world is calling, but preparation has become an excuse to stay", 1),
    ("wands-four", "Four of Wands", "celebration, belonging, a stable milestone", "instability, feeling unwelcome, private conflict", "Nahida's welcome after an Akademiya success offers earned belonging without ownership", "achievement cannot feel like home while praise is still distrusted", 1),
    ("wands-five", "Five of Wands", "competition, friction, lively disagreement", "resentment, conflict avoidance, needless chaos", "sparring with allies turns conflict into practice rather than betrayal", "every challenge is being treated as an attack, so useful friction becomes damage", -1),
    ("wands-six", "Six of Wands", "recognition, victory, visible progress", "hollow praise, self-doubt, a private fall", "Wanderer's return beneath teal banners marks success claimed in his own name", "applause cannot repair a victory pursued only to prove worth", 1),
    ("wands-seven", "Seven of Wands", "defence, conviction, holding ground", "exhaustion, defensiveness, surrendering ground", "seven Fatui shadows test whether the new identity can defend its boundaries", "constant defence has made every shadow look like an enemy", 0),
    ("wands-eight", "Eight of Wands", "swift movement, messages, aligned momentum", "delay, scattered energy, missed timing", "eight Anemo trails show decisive motion once the direction is clear", "speed has scattered attention, or the necessary message is stalled", 1),
    ("wands-nine", "Nine of Wands", "resilience, vigilance, the final test", "burnout, suspicion, refusing rest", "wounded Wanderer remains standing; endurance is real, but it is not infinite", "survival has become hypervigilance and rest feels more dangerous than battle", 0),
    ("wands-ten", "Ten of Wands", "burden, responsibility, overextension", "release, delegation, collapse under pressure", "ten masks make every former identity feel like a duty that must be carried", "the masks can be set down; not every past self requires lifelong punishment", -1),
    ("wands-page", "Page of Wands", "curiosity, discovery, enthusiastic news", "hesitation, immaturity, a blocked spark", "Kabukimono discovering the forge remembers that wonder existed before ambition", "curiosity is being mocked or hidden before it has room to grow", 1),
    ("wands-knight", "Knight of Wands", "bold pursuit, adventure, passionate action", "recklessness, impatience, unstable momentum", "Wanderer's storm-flight commits fully to a direction and refuses paralysis", "velocity is substituting for judgment and risks repeating an old fall", 0),
    ("wands-queen", "Queen of Wands", "confidence, independence, magnetic competence", "jealousy, pride, brittle confidence", "Faruzan commands ancient mechanisms because expertise has been earned and owned", "knowledge is being used to demand admiration rather than solve the problem", 1),
    ("wands-king", "King of Wands", "vision, leadership, inspired independence", "domination, impulsive leadership, empty charisma", "Venti's untamed wind frames freedom as something offered, never imposed", "freedom is being performed while another person's choice is quietly controlled", 1),

    ("cups-ace", "Ace of Cups", "emotional opening, compassion, intuitive renewal", "blocked feeling, emptiness, emotional overflow", "Kabukimono's first tear proves a heart can be experienced before it is understood", "feeling is being treated as weakness, leaving the cup full but untouched", 1),
    ("cups-two", "Two of Cups", "mutual trust, partnership, emotional exchange", "miscommunication, imbalance, separation", "Kabukimono and Niwa exchange trust beside the furnace without demanding ownership", "a once-mutual bond is being distorted by silence or unequal sacrifice", 1),
    ("cups-three", "Three of Cups", "friendship, celebration, shared support", "exclusion, excess, fractured community", "Niwa, Katsuragi, and Kabukimono show the found family Tatarasuna briefly became", "belonging is threatened by isolation, gossip, or the belief that joy cannot last", 1),
    ("cups-four", "Four of Cups", "withdrawal, contemplation, overlooked help", "renewed interest, restless dissatisfaction", "Scaramouche stares past offered comfort toward the Gnosis he believes will complete him", "the fixation loosens, but accepting an ordinary kindness still feels dangerous", 0),
    ("cups-five", "Five of Cups", "grief, regret, attention fixed on loss", "acceptance, recovery, seeing what remains", "the three betrayals fill the whole field of vision while surviving bonds go unseen", "grief is beginning to turn around and notice what loss did not destroy", -1),
    ("cups-six", "Six of Cups", "memory, innocence, the emotional past", "being trapped in nostalgia, distorted memory", "the sick child and handmade doll recall tenderness untouched by rank or ambition", "memory has become an idealized room that the present can never satisfy", 0),
    ("cups-seven", "Seven of Cups", "possibilities, fantasy, difficult selection", "clarity, commitment, illusion breaking", "seven reflected identities tempt Wanderer to become whichever mask promises safety", "the mirrors are clearing and one honest identity can finally be chosen", 0),
    ("cups-eight", "Eight of Cups", "walking away, seeking truth, emotional transition", "avoidance, fear of leaving, returning to harm", "Kabukimono leaves ruined Tatarasuna because remaining would mean living inside the wound", "departure is being delayed, or walking away is used to avoid necessary grief", 0),
    ("cups-nine", "Nine of Cups", "a wish, satisfaction, emotional attainment", "overindulgence, hollow fulfilment, disappointment", "Shouki no Kami reaches for the wish of possessing a heart and being acknowledged", "the wish was built from absence, so obtaining it cannot create wholeness", 1),
    ("cups-ten", "Ten of Cups", "lasting connection, harmony, chosen family", "fractured bonds, unrealistic harmony, estrangement", "Nahida, Traveler, and Paimon offer a flawed family that leaves room for sharp edges", "belonging is being rejected because it cannot resemble a perfect ending", 1),
    ("cups-page", "Page of Cups", "gentle curiosity, intuition, an emotional message", "emotional immaturity, insecurity, ignored intuition", "Kabukimono's wonder at a tiny bird invites emotion without embarrassment", "a tender impulse is being dismissed before it can speak clearly", 1),
    ("cups-knight", "Knight of Cups", "compassionate pursuit, loyalty, heartfelt action", "idealization, moodiness, promises without action", "Niwa carries kindness through danger and proves loyalty through what he does", "a beautiful promise lacks the action that would make it trustworthy", 1),
    ("cups-queen", "Queen of Cups", "empathy, emotional wisdom, intuitive care", "emotional overwhelm, dependency, concealed hurt", "Nahida holds painful memories without controlling the person who owns them", "care has absorbed too much pain or crossed the boundary into rescue", 1),
    ("cups-king", "King of Cups", "emotional balance, diplomacy, steady compassion", "emotional control, volatility, concealed manipulation", "present Wanderer feels deeply without surrendering judgment or sharpness", "composure has become a mask that keeps every honest feeling unreachable", 1),

    ("swords-ace", "Ace of Swords", "clarity, truth, a decisive realization", "confusion, misinformation, a truth misused", "the blade of truth cuts Dottore's manufactured story away from Niwa's fate", "a partial truth is being sharpened into another weapon", 1),
    ("swords-two", "Two of Swords", "stalemate, difficult choice, guarded peace", "indecision breaking, overload, denial", "Kabukimono and Scaramouche face each other across the choice to integrate or reject", "the stalemate can no longer be maintained; avoidance is choosing by default", 0),
    ("swords-three", "Three of Swords", "heartbreak, sorrow, painful separation", "recovery, forgiveness, pain held too long", "Ei, Niwa, and the child name three wounds that shaped one defensive heart", "old grief is ready to move, but reopening it for identity keeps it alive", -1),
    ("swords-four", "Four of Swords", "rest, retreat, recovery, contemplation", "restlessness, burnout, forced pause", "sleep inside Shakkei Pavilion is a necessary silence before the world begins", "the sealed room has become stagnation, or rest is refused until collapse", 0),
    ("swords-five", "Five of Swords", "conflict, hollow victory, winning at a cost", "reconciliation, lingering resentment, accountability", "the Balladeer's Fatui victory asks what remains after everyone else has been diminished", "the cost of winning is finally visible, creating a narrow path toward repair", -1),
    ("swords-six", "Six of Swords", "transition, leaving difficulty, a sober passage", "unfinished baggage, resisted transition, return", "crossing dark water away from Inazuma carries pain forward but refuses to remain trapped", "distance changed the scenery, not the wound that travelled with it", 0),
    ("swords-seven", "Seven of Swords", "strategy, secrecy, acting alone", "exposure, confession, self-deception", "the nameless Wanderer enters Irminsul alone, using secrecy to rewrite an unbearable truth", "the hidden act is surfacing, especially the lie told to the self", -1),
    ("swords-eight", "Eight of Swords", "restriction, helplessness, limiting beliefs", "release, new perspective, reclaiming agency", "Fatui experiments and puppet strings turn imposed limits into an internal cage", "one binding belief has loosened; movement begins by testing the boundary", -1),
    ("swords-nine", "Nine of Swords", "anxiety, nightmares, private despair", "recovery, naming fear, seeking support", "the furnace, child, and betrayals return at night when control cannot silence them", "the nightmare weakens once it is spoken and separated from the present", -1),
    ("swords-ten", "Ten of Swords", "painful ending, defeat, rock bottom", "survival, recovery, resisting a necessary ending", "the false god lies beneath broken blades; the constructed divinity has ended completely", "the fall is over, but the mind keeps reenacting impact instead of beginning recovery", -1),
    ("swords-page", "Page of Swords", "curiosity, vigilance, difficult news", "gossip, suspicion, careless words", "Childe's suspicion around the stolen Gnosis notices the inconsistency others ignore", "watchfulness has tipped into rumor or accusation without enough evidence", 0),
    ("swords-knight", "Knight of Swords", "decisive action, ambition, charging ahead", "aggression, poor timing, reckless certainty", "La Signora's advance toward Inazuma shows the force and danger of unwavering purpose", "certainty is charging faster than the facts can support", -1),
    ("swords-queen", "Queen of Swords", "clear boundaries, independence, honest judgment", "cruelty, bitterness, cold isolation", "Yae Miko bargains away the Gnosis with ruthless clarity about the real priority", "clarity has lost compassion and now cuts simply because it can", 0),
    ("swords-king", "King of Swords", "intellect, authority, strategic truth", "abuse of intellect, manipulation, tyranny", "Dottore's laboratory warns that intelligence without conscience turns lives into materials", "cold strategy is controlling the story while pretending to be neutral", -1),

    ("pentacles-ace", "Ace of Pentacles", "a tangible opportunity, grounding, material potential", "missed opportunity, instability, poor foundation", "Ei's golden feather is proof of origin and a concrete resource for the road ahead", "the symbol of worth is being mistaken for worth itself", 1),
    ("pentacles-two", "Two of Pentacles", "balance, adaptation, managing competing demands", "overload, disorganization, a dropped priority", "Wanderer balances Kabukimono and Balladeer masks without letting either consume the present", "the former selves are competing so loudly that present needs are being dropped", 0),
    ("pentacles-three", "Three of Pentacles", "teamwork, craft, shared mastery", "poor collaboration, uneven skill, ignored contribution", "Kabukimono, Niwa, and Katsuragi build something no one of them could make alone", "the forge fails when pride erases one person's necessary contribution", 1),
    ("pentacles-four", "Four of Pentacles", "security, possession, holding control", "release, generosity, fear of loss", "Scaramouche grips the Gnosis because possession has become proof that he cannot be discarded", "the grip is loosening, though loss still feels identical to worthlessness", 0),
    ("pentacles-five", "Five of Pentacles", "hardship, abandonment, exclusion", "support, recovery, an open door", "the puppet waits outside the sealed domain and interprets abandonment as a verdict on existence", "shelter is closer than expected, but accepting it requires looking up", -1),
    ("pentacles-six", "Six of Pentacles", "giving, receiving, fair support", "strings attached, unequal exchange, debt", "Nahida offers meaningful work and a second chance without demanding gratitude as payment", "help comes with control, or pride refuses an exchange that could be fair", 1),
    ("pentacles-seven", "Seven of Pentacles", "patience, assessment, long-term cultivation", "impatience, wasted effort, abandoning growth", "tending the Irminsul sapling values slow repair whose results cannot be forced", "growth is being judged too early, tempting the gardener to uproot it", 1),
    ("pentacles-eight", "Eight of Pentacles", "practice, discipline, apprenticeship", "perfectionism, boredom, careless work", "Vahumana reports turn reluctant study into the daily craft of rebuilding a life", "work has become punishment or detail is being pursued without purpose", 1),
    ("pentacles-nine", "Nine of Pentacles", "independence, earned comfort, self-sufficiency", "isolation, unstable independence, status anxiety", "Wanderer in the Sumeru garden enjoys solitude chosen freely rather than imposed", "self-sufficiency has become a wall that no trustworthy person may cross", 1),
    ("pentacles-ten", "Ten of Pentacles", "legacy, lasting foundation, intergenerational consequence", "fractured legacy, unstable inheritance, family conflict", "Ei, Kabukimono, Balladeer, Wanderer, and Nahida form a lineage that can be understood without being idealized", "inheritance is repeating because its fractures are hidden behind a perfect family picture", 1),
    ("pentacles-page", "Page of Pentacles", "study, practical opportunity, a grounded message", "procrastination, poor follow-through, missed learning", "Layla's notes invite the reluctant student to treat knowledge as usable rather than ornamental", "the lesson is available, but embarrassment or distraction prevents beginning", 1),
    ("pentacles-knight", "Knight of Pentacles", "steady progress, responsibility, reliable effort", "stagnation, stubborn routine, careless work", "Traveler carries truth between nations through persistence rather than spectacle", "routine continues after it stops serving the destination", 1),
    ("pentacles-queen", "Queen of Pentacles", "grounded care, practical abundance, capable stewardship", "smothering care, insecurity, neglected needs", "Ei holding the feather asks creation to include practical responsibility for what is made", "care remains symbolic while the created person bears the real consequences alone", 1),
    ("pentacles-king", "King of Pentacles", "stability, stewardship, lasting achievement", "control through resources, greed, brittle security", "Nahida safeguards Irminsul and gives Wanderer's new foundation room to take root", "protection has become ownership, or stability is maintained by refusing change", 1),
)

TAROT_DECK = tuple(TarotCard(*row) for row in CARD_ROWS)
if len(TAROT_DECK) != 78 or len({card.stem for card in TAROT_DECK}) != 78:
    raise RuntimeError("The Wanderer tarot definition must contain 78 unique cards")
CARD_BY_STEM = {card.stem: card for card in TAROT_DECK}

SPREADS = {
    "celtic_cross": Spread(
        "celtic_cross", "Celtic Cross Spread",
        ("Present", "Challenge", "Foundation", "Recent past", "Possibility", "Near future",
         "Your stance", "Environment", "Hopes and fears", "Likely outcome"),
        420, 560,
    ),
    "three_card": Spread("three_card", "Three-Card Spread", ("Past", "Present", "Future"), 210, 450),
    "yes_no": Spread(
        "yes_no", "Five-Card Yes/No Spread",
        ("Present answer", "Supporting factor", "Resisting factor", "Hidden influence", "Likely outcome"),
        270, 540,
    ),
}

CELTIC_CROSS_MESSAGE_GROUPS = ((0, 1, 2, 3), (4, 5, 6), (7, 8, 9))
DISCORD_CONTENT_LIMIT = 1950


class TarotStore:
    """Small independent SQLite store for opt-in readings, preferences, and daily cards."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            configured = (os.getenv("TAROT_DB_PATH") or "").strip()
            if configured:
                path = Path(configured).expanduser()
            else:
                data_dir = Path((os.getenv("BOT_DATA_DIR") or os.getenv("MEMORY_DATA_DIR") or "").strip() or Path(__file__).resolve().parent / "data")
                path = data_dir / "tarot.sqlite3"
        self.path = Path(path)
        self._ready = False
        self._init_lock = None
        self.lease = None
        self.guard = None

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            if self.lease is not None:
                uid, bot_name, token = self.lease
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT token,expires FROM tarot_sessions WHERE user_id=? AND bot_name=?",
                    (uid, bot_name),
                ).fetchone()
                if not row or row["token"] != token or row["expires"] < time.time():
                    raise PermissionError("Tarot session expired; open a new table.")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_sync(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tarot_preferences (
                    user_id INTEGER PRIMARY KEY,
                    reversals INTEGER NOT NULL DEFAULT 1,
                    visibility TEXT NOT NULL DEFAULT 'public',
                    detail TEXT NOT NULL DEFAULT 'brief',
                    meaning_mode TEXT NOT NULL DEFAULT 'lore',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tarot_daily (
                    user_id INTEGER NOT NULL,
                    bot_name TEXT NOT NULL,
                    draw_date TEXT NOT NULL,
                    card_stem TEXT NOT NULL,
                    reversed INTEGER NOT NULL,
                    reading TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, bot_name, draw_date)
                );
                CREATE TABLE IF NOT EXISTS tarot_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    bot_name TEXT NOT NULL,
                    question TEXT NOT NULL,
                    spread_key TEXT NOT NULL,
                    cards_json TEXT NOT NULL,
                    reading TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tarot_history_user_idx
                    ON tarot_history(user_id, id DESC);
                CREATE TABLE IF NOT EXISTS tarot_sessions (
                    user_id INTEGER NOT NULL, bot_name TEXT NOT NULL,
                    token TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(user_id,bot_name)
                );
                """
            )

    async def initialize(self):
        if self._ready:
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if not self._ready:
                await asyncio.to_thread(self._initialize_sync)
                self._ready = True

    async def session(self, user_id, bot_name, guard=None):
        """Bind every read/write to a revocable shared-store session."""
        await self.initialize()
        token = secrets.token_urlsafe(24)
        def create():
            with self._connect() as db:
                db.execute("DELETE FROM tarot_sessions WHERE expires<?", (time.time(),))
                db.execute(
                    "INSERT OR REPLACE INTO tarot_sessions VALUES(?,?,?,?)",
                    (int(user_id), bot_name.lower(), token, time.time() + 1800),
                )
        await asyncio.to_thread(create)
        scoped = TarotStore(self.path)
        scoped._ready = True
        scoped.lease = (int(user_id), bot_name.lower(), token)
        scoped.guard = guard
        return scoped

    async def validate(self, text=""):
        if self.guard:
            await self.guard(text)
        def check():
            with self._connect():
                pass
        await asyncio.to_thread(check)

    async def forget(self, user_id, topic=None):
        """Invalidate open tables in BOTH processes before deleting user data."""
        await self.initialize()
        def erase():
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM tarot_sessions WHERE user_id=?", (int(user_id),))
                before = db.total_changes
                if topic:
                    pattern = "%" + topic.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
                    db.execute("DELETE FROM tarot_history WHERE user_id=? AND (lower(question) LIKE ? ESCAPE '!' OR lower(reading) LIKE ? ESCAPE '!')",
                               (int(user_id), pattern, pattern))
                    db.execute("DELETE FROM tarot_daily WHERE user_id=? AND lower(reading) LIKE ? ESCAPE '!'",
                               (int(user_id), pattern))
                else:
                    for table in ("tarot_preferences", "tarot_daily", "tarot_history"):
                        db.execute("DELETE FROM " + table + " WHERE user_id=?", (int(user_id),))
                return db.total_changes - before
        return await asyncio.to_thread(erase)

    async def get_preferences(self, user_id: int) -> TarotPreferences:
        await self.initialize()
        def read():
            with self._connect() as db:
                return db.execute(
                    "SELECT reversals, visibility, detail, meaning_mode FROM tarot_preferences WHERE user_id=?",
                    (int(user_id),),
                ).fetchone()
        row = await asyncio.to_thread(read)
        if not row:
            return TarotPreferences()
        return TarotPreferences(bool(row["reversals"]), row["visibility"], row["detail"], row["meaning_mode"])

    async def set_preference(self, user_id: int, key: str, value) -> TarotPreferences:
        allowed = {
            "reversals": {0, 1, False, True},
            "visibility": {"public", "private"},
            "detail": {"brief", "detailed"},
            "meaning_mode": {"lore", "standard"},
        }
        if key not in allowed or value not in allowed[key]:
            raise ValueError(f"Invalid tarot preference: {key}={value!r}")
        current = await self.get_preferences(user_id)
        values = {
            "reversals": int(bool(current.reversals)),
            "visibility": current.visibility,
            "detail": current.detail,
            "meaning_mode": current.meaning_mode,
        }
        values[key] = int(bool(value)) if key == "reversals" else value
        now = datetime.now(timezone.utc).isoformat()
        def write():
            with self._connect() as db:
                db.execute(
                    """INSERT INTO tarot_preferences(user_id,reversals,visibility,detail,meaning_mode,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET reversals=excluded.reversals,
                       visibility=excluded.visibility, detail=excluded.detail,
                       meaning_mode=excluded.meaning_mode, updated_at=excluded.updated_at""",
                    (int(user_id), values["reversals"], values["visibility"], values["detail"], values["meaning_mode"], now),
                )
        await asyncio.to_thread(write)
        return TarotPreferences(bool(values["reversals"]), values["visibility"], values["detail"], values["meaning_mode"])

    async def get_daily(self, user_id: int, bot_name: str, draw_date: str):
        await self.initialize()
        def read():
            with self._connect() as db:
                row = db.execute(
                    "SELECT card_stem,reversed,reading FROM tarot_daily WHERE user_id=? AND bot_name=? AND draw_date=?",
                    (int(user_id), bot_name.lower(), draw_date),
                ).fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(read)

    async def create_daily(self, user_id: int, bot_name: str, draw_date: str, item: DrawnCard):
        await self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        def write():
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO tarot_daily(user_id,bot_name,draw_date,card_stem,reversed,created_at) VALUES(?,?,?,?,?,?)",
                    (int(user_id), bot_name.lower(), draw_date, item.card.stem, int(item.reversed), now),
                )
        await asyncio.to_thread(write)
        return await self.get_daily(user_id, bot_name, draw_date)

    async def set_daily_reading(self, user_id: int, bot_name: str, draw_date: str, reading: str):
        await self.initialize()
        def write():
            with self._connect() as db:
                db.execute(
                    "UPDATE tarot_daily SET reading=? WHERE user_id=? AND bot_name=? AND draw_date=?",
                    (reading[:5000], int(user_id), bot_name.lower(), draw_date),
                )
        await asyncio.to_thread(write)

    async def save_reading(self, user_id: int, bot_name: str, question: str, spread_key: str, draws: list[DrawnCard], reading: str):
        await self.initialize()
        payload = json.dumps([
            {"stem": item.card.stem, "position": item.position, "reversed": item.reversed}
            for item in draws
        ], separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        def write():
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO tarot_history(user_id,bot_name,question,spread_key,cards_json,reading,created_at) VALUES(?,?,?,?,?,?,?)",
                    (int(user_id), bot_name, question[:500], spread_key, payload, reading[:6000], now),
                )
                return int(cursor.lastrowid)
        return await asyncio.to_thread(write)

    async def get_history(self, user_id: int, limit: int = 5):
        await self.initialize()
        def read():
            with self._connect() as db:
                rows = db.execute(
                    "SELECT id,bot_name,question,spread_key,cards_json,created_at FROM tarot_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
                    (int(user_id), max(1, min(10, int(limit)))),
                ).fetchall()
                return [dict(row) for row in rows]
        return await asyncio.to_thread(read)


_STORE: TarotStore | None = None


def get_tarot_store() -> TarotStore:
    global _STORE
    if _STORE is None:
        _STORE = TarotStore()
    return _STORE


def resolve_deck_dir() -> Path:
    configured = (os.getenv("TAROT_DECK_DIR") or "").strip()
    candidates: list[Path] = [Path(configured).expanduser()] if configured else []
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        candidates.extend((base / "assets" / "tarot" / "wanderer" / "bot_deck", base / "assets" / "tarot" / "wanderer" / "full_deck"))
    for candidate in candidates:
        if candidate.is_dir() and sum(bool(card_path(candidate, card.stem)) for card in TAROT_DECK) == 78:
            return candidate
    raise FileNotFoundError("The 78-card Wanderer tarot art folder is missing")


def card_path(deck_dir: Path, stem: str) -> Path | None:
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        path = deck_dir / f"{stem}{suffix}"
        if path.is_file():
            return path
    return None


def draw_spread(spread_key: str, *, reversals: bool = True, exclude: set[str] | None = None, rng: random.Random | None = None) -> list[DrawnCard]:
    spread = SPREADS[spread_key]
    picker = rng or random.SystemRandom()
    excluded = exclude or set()
    available = [card for card in TAROT_DECK if card.stem not in excluded]
    selected = picker.sample(available, len(spread.positions))
    return [DrawnCard(card, position, bool(reversals and picker.random() < 0.35)) for card, position in zip(selected, spread.positions)]


def draw_one(position: str, *, reversals: bool = True, exclude: set[str] | None = None, rng: random.Random | None = None) -> DrawnCard:
    picker = rng or random.SystemRandom()
    excluded = exclude or set()
    card = picker.choice([item for item in TAROT_DECK if item.stem not in excluded])
    return DrawnCard(card, position, bool(reversals and picker.random() < 0.35))


def yes_no_analysis(draws: list[DrawnCard]) -> YesNoAnalysis:
    weights = (1.0, 1.0, -1.0, 0.5, 2.25)
    signals = []
    score = 0.0
    for index, item in enumerate(draws[:5]):
        signal = float(item.card.polarity)
        if item.reversed:
            signal *= -1
        weighted = signal * weights[index]
        signals.append(weighted)
        score += weighted
    positive = sum(value > 0 for value in signals)
    warning = sum(value < 0 for value in signals)
    uncertain = len(signals) - positive - warning
    if score >= 3:
        label, explanation = "Strong Yes", "several cards support movement, and the outcome card reinforces it"
    elif score >= 1:
        label, explanation = "Leaning Yes", "the opening is real, though it still needs deliberate action"
    elif score <= -3:
        label, explanation = "Strong No", "resistance dominates, and the likely outcome warns against forcing it"
    elif score <= -1:
        label, explanation = "Leaning No", "the current pattern resists this more than it supports it"
    else:
        label, explanation = "Unclear / Not Yet", "the influences are divided and the answer is still being shaped"
    return YesNoAnalysis(label, explanation, score, positive, uncertain, warning)


def reading_limits(spread_key: str, preferences: TarotPreferences) -> tuple[int, int]:
    spread = SPREADS[spread_key]
    if preferences.detail == "detailed":
        return int(spread.brief_words * 1.55), min(760, int(spread.brief_tokens * 1.45))
    return spread.brief_words, spread.brief_tokens


def draw_summary(draws: list[DrawnCard]) -> str:
    return "\n".join(f"{index}. **{item.position}:** {item.card.name} ({item.orientation})" for index, item in enumerate(draws, 1))


def fallback_reading(
    spread_key: str,
    draws: list[DrawnCard],
    preferences: TarotPreferences | None = None,
    *,
    question: str = "",
) -> str:
    """Complete, grounded reading when generation is unavailable or unfinished."""
    preferences = preferences or TarotPreferences()
    lines = []
    if question.strip():
        lines.append(
            "For your question, the cards offer a symbolic perspective, not a factual "
            "claim about the past or a certain prediction of the future."
        )
    if spread_key == "yes_no":
        result = yes_no_analysis(draws)
        lines.extend((f"**Verdict: {result.label}.** {result.explanation}.", f"Signal: {result.positive} positive · {result.uncertain} uncertain · {result.warning} warning."))
    for item in draws:
        lines.append(f"**{item.position} ({item.card.name}, {item.orientation}):** {item.meaning(preferences.meaning_mode).capitalize()}.")
    if spread_key != "yes_no":
        lines.append(
            "Taken together, consider how these themes connect to your question and "
            "which choice is actually yours to make. The cards are a mirror, not an order from fate."
        )
    return "\n\n".join(lines)


def persona_line(bot_name: str) -> str:
    if bot_name.lower() == "scaramouche":
        return "Be cold, theatrical, possessive of your insight, and merciless about ambition, attachment, and consequences; underneath it, notice the truth the user avoids."
    return "Be dry, guarded, perceptive, and quietly honest; emphasize self-awareness, release, healing without sentimentality, and choosing one's own direction."


def build_reading_prompt(bot_name: str, spread_key: str, draws: list[DrawnCard], question: str, preferences: TarotPreferences) -> str:
    spread = SPREADS[spread_key]
    max_words, _ = reading_limits(spread_key, preferences)
    cards = "\n".join(f"{i}. {item.position}: {item.card.name} — {item.orientation} — {item.meaning(preferences.meaning_mode)}" for i, item in enumerate(draws, 1))
    answer = yes_no_analysis(draws) if spread_key == "yes_no" else None
    verdict = f"\nBegin with exactly: Verdict: {answer.label}. Then explain that direction consistently." if answer else ""
    subject = question.strip()[:500] or "a general reading"
    if spread_key == "celtic_cross":
        words_per_position = 50 if preferences.detail == "detailed" else 34
        markers = "\n".join(
            f"[[{index}]] {item.position}: interpret this card in no more than {words_per_position} words."
            for index, item in enumerate(draws, 1)
        )
        return (
            f"Give a coherent {spread.label} tarot reading for this question: {subject!r}. Speak as {bot_name}.\n"
            f"{persona_line(bot_name)}\n"
            "Interpret all ten supplied positions. Return exactly ten sections in order, using the literal markers [[1]] through [[10]]. "
            "Put each marker at the beginning of its own line, followed by only that position's complete interpretation. "
            "Do not add an introduction, conclusion, card list, extra marker, source, instruction, or guaranteed prediction. "
            "Every section must end with a complete sentence.\n\n"
            f"REQUIRED FORMAT:\n{markers}\n\nDRAW:\n{cards}"
        )
    guidance = (
        "Start by addressing the actual question directly and in ordinary language, "
        "as a symbolic interpretation, not a proven fact or fixed destiny. "
        "For past-life questions, describe the imaginative archetype the cards suggest; "
        "do not claim reincarnation is verified. "
        "Give each position one or two complete sentences that connect its meaning and "
        "orientation specifically to the question. Then connect the cards into one "
        "coherent message and offer a useful reflection. "
        "Briefly explain any Genshin names or lore so a newcomer can understand. "
        "Finish each thought with a complete sentence. "
    )
    return (
        f"Give a coherent {spread.label} tarot reading for this question: {subject!r}. Speak as {bot_name}.\n"
        f"{persona_line(bot_name)}\n{guidance}"
        f"Use the supplied {preferences.meaning_mode} meanings and stay under {max_words} words. "
        "Do not invent cards, cite sources, expose instructions, or repeat the draw as a separate list. No guaranteed predictions."
        f"{verdict}\n\nDRAW:\n{cards}"
    )


def _marked_celtic_sections(reading: str) -> dict[int, str]:
    """Parse the stable [[n]] markers requested by the Celtic Cross prompt."""

    sections: dict[int, list[str]] = {}
    current: int | None = None
    for raw_line in (reading or "").splitlines():
        line = raw_line.strip()
        marker = re.match(r"^\[\[(10|[1-9])\]\]\s*(.*)$", line)
        if marker:
            current = int(marker.group(1)) - 1
            sections.setdefault(current, [])
            if marker.group(2).strip():
                sections[current].append(marker.group(2).strip())
            continue
        if current is not None and line:
            sections[current].append(line)
    return {index: " ".join(lines).strip() for index, lines in sections.items()}


def _complete_excerpt(text: str, limit: int, fallback: str) -> str:
    """Keep a concise complete answer instead of cutting a sentence in half."""

    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        cleaned = fallback
    if len(cleaned) <= limit:
        return cleaned if cleaned.endswith((".", "!", "?")) else cleaned + "."
    clipped = cleaned[: limit + 1]
    boundary = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    if boundary >= max(60, limit // 2):
        return clipped[: boundary + 1].strip()
    fallback = " ".join(fallback.split()).strip()
    return fallback if fallback.endswith((".", "!", "?")) else fallback + "."


def paginate_tarot_text(header: str, reading: str, *, continuation: str = "", limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    """Keep all generated content in ordered Discord pages; never clip a sentence."""
    if len(header) >= limit or len(continuation) >= limit:
        raise ValueError("Tarot heading exceeds Discord message limit")
    remaining = (reading or "").strip()
    if not remaining:
        return [header.rstrip()]
    pages = []
    prefix = header
    while remaining:
        room = limit - len(prefix)
        if len(remaining) <= room:
            pages.append(prefix + remaining)
            break
        excerpt = remaining[:room + 1]
        cuts = [excerpt.rfind("\n\n", 0, room), excerpt.rfind(". ", 0, room),
                excerpt.rfind("! ", 0, room), excerpt.rfind("? ", 0, room)]
        cut = max(cuts)
        if cut >= max(30, room // 2):
            cut += 2
        else:
            cut = max(excerpt.rfind(" ", 0, room), excerpt.rfind("\n", 0, room))
            if cut < max(30, room // 2):
                cut = room
        pages.append(prefix + remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
        prefix = continuation
    return pages


def celtic_cross_pages(
    bot_name: str,
    question: str,
    draws: list[DrawnCard],
    reading: str,
    preferences: TarotPreferences,
) -> list[str]:
    """Format a Celtic Cross as the three ordered Discord messages users expect."""

    if len(draws) != 10:
        raise ValueError("A Celtic Cross reading requires exactly ten cards")
    parsed = _marked_celtic_sections(reading)
    pages: list[str] = []
    for page_number, indexes in enumerate(CELTIC_CROSS_MESSAGE_GROUPS, 1):
        heading = f"🔮 **{bot_name}'s Celtic Cross · Part {page_number}/3**"
        if page_number == 1 and question:
            heading += f"\n*Question: {' '.join(question.split())[:500]}*"
        section_headings = [
            f"**{index + 1}. {draws[index].position}: {draws[index].card.name} ({draws[index].orientation})**"
            for index in indexes
        ]
        fixed_length = len(heading) + sum(len(value) for value in section_headings) + (len(indexes) * 4)
        per_section_limit = max(180, (DISCORD_CONTENT_LIMIT - fixed_length) // len(indexes))
        blocks = []
        for index, section_heading in zip(indexes, section_headings):
            item = draws[index]
            fallback = item.meaning(preferences.meaning_mode).capitalize()
            generated = parsed.get(index, "")
            generated = re.sub(
                rf"^(?:\*\*)?{re.escape(item.position)}(?:\*\*)?\s*[:—-]\s*",
                "",
                generated,
                flags=re.IGNORECASE,
            )
            explanation = _complete_excerpt(generated, per_section_limit, fallback)
            blocks.append(f"{section_heading}\n{explanation}")
        page = heading + "\n\n" + "\n\n".join(blocks)
        if len(page) > 2000:
            raise RuntimeError("Celtic Cross page exceeded Discord's message limit")
        pages.append(page)
    return pages


def build_daily_prompt(bot_name: str, item: DrawnCard, preferences: TarotPreferences) -> str:
    return (
        f"Give a daily tarot reflection as {bot_name} in 90 words or fewer. {persona_line(bot_name)} "
        f"Card: {item.card.name}, {item.orientation}. Meaning: {item.meaning(preferences.meaning_mode)}. "
        "Give one theme and one practical focus for today. Do not cite sources or promise fate."
    )


def build_clarifier_prompt(bot_name: str, original_question: str, draws: list[DrawnCard], clarifier: DrawnCard, preferences: TarotPreferences) -> str:
    original = "; ".join(f"{item.position}: {item.card.name} {item.orientation}" for item in draws)
    return (
        f"As {bot_name}, explain one clarifying tarot card in 130 words or fewer. {persona_line(bot_name)} "
        f"Question: {original_question or 'general reading'}. Original draw: {original}. "
        f"Clarifier: {clarifier.card.name}, {clarifier.orientation}; meaning: {clarifier.meaning(preferences.meaning_mode)}. "
        "Connect this clarifier to at least one of the original cards and directly address the person's original question. "
        "Explain any lore references in plain language. Give one useful reflection and end in a complete sentence. "
        "Treat past lives as symbolic storytelling, not verified history. No sources or guaranteed predictions."
    )


def build_explain_prompt(bot_name: str, question: str, item: DrawnCard, preferences: TarotPreferences) -> str:
    return (
        f"As {bot_name}, explain this tarot position in 150 words or fewer. {persona_line(bot_name)} "
        f"Question: {question or 'general reading'}. Position: {item.position}. Card: {item.card.name}, {item.orientation}. "
        f"Meaning: {item.meaning(preferences.meaning_mode)}. Explain how it relates to the original question in plain language, "
        "including a caution and a complete conclusion. Explain lore if mentioned. No sources."
    )


def build_followup_prompt(bot_name: str, original_question: str, followup: str, draws: list[DrawnCard], reading: str, preferences: TarotPreferences) -> str:
    cards = "; ".join(f"{item.position}: {item.card.name} {item.orientation}" for item in draws)
    return (
        f"Answer a follow-up about an existing tarot reading as {bot_name} in 180 words or fewer. {persona_line(bot_name)} "
        f"Original question: {original_question or 'general reading'}. Cards: {cards}. Earlier reading: {reading[:900]}. "
        f"Follow-up: {followup[:400]}. Answer the follow-up directly and clearly using the same cards, "
        "connect it to the original question, and end in a complete sentence. Explain lore if needed. "
        "Add no cards, cite no sources, and do not guarantee fate."
    )


def reveal_reaction(bot_name: str, item: DrawnCard, preferences: TarotPreferences) -> str:
    meaning = item.meaning(preferences.meaning_mode)
    if bot_name.lower() == "scaramouche":
        return f"**{item.position}: {item.card.name} ({item.orientation}).** {meaning.capitalize()}. Tch. Don't look away now."
    return f"**{item.position}: {item.card.name} ({item.orientation}).** {meaning.capitalize()}. Sit with it before reaching for an easier answer."


def preferences_text(bot_name: str, preferences: TarotPreferences) -> str:
    return (
        f"**{bot_name}'s tarot settings**\n"
        f"Reversed cards: **{'On' if preferences.reversals else 'Off'}**\n"
        f"Visibility: **{preferences.visibility.title()}**\n"
        f"Reading detail: **{preferences.detail.title()}**\n"
        f"Meanings: **{preferences.meaning_mode.title()}**\n\n"
        "Press a button to change one setting. These choices are remembered."
    )


def history_text(rows: list[dict]) -> str:
    if not rows:
        return "You have no saved tarot readings yet. Use the **Save reading** button after a spread."
    lines = ["**Your saved tarot readings**"]
    for row in rows:
        try:
            cards = json.loads(row.get("cards_json") or "[]")
        except Exception:
            cards = []
        names = [CARD_BY_STEM[item.get("stem")].name for item in cards if item.get("stem") in CARD_BY_STEM]
        date = (row.get("created_at") or "")[:10]
        spread = SPREADS.get(row.get("spread_key"), Spread("", row.get("spread_key", "Tarot"), (), 0, 0)).label
        question = (row.get("question") or "General reading").strip() or "General reading"
        card_line = ", ".join(names[:5]) + (f" +{len(names)-5} more" if len(names) > 5 else "")
        lines.append(f"`#{row.get('id')}` **{date} · {row.get('bot_name')} · {spread}**\n{question[:120]}\n{card_line}")
    return "\n\n".join(lines)[:1950]


def _font(size: int, *, bold: bool = False):
    names = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _card_face(item: DrawnCard, deck_dir: Path, size: tuple[int, int], *, rotate_cross: bool = False) -> Image.Image:
    path = card_path(deck_dir, item.card.stem)
    if path is None:
        raise FileNotFoundError(f"Missing tarot card art: {item.card.stem}")
    with Image.open(path) as source:
        face = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    if item.reversed:
        face = face.rotate(180)
    if rotate_cross:
        face = face.rotate(90, expand=True)
    return face


def _card_back(size: tuple[int, int], *, rotate_cross: bool = False) -> Image.Image:
    width, height = size
    back = Image.new("RGB", size, "#091a2d")
    draw = ImageDraw.Draw(back)
    draw.rounded_rectangle((5, 5, width - 6, height - 6), radius=14, outline="#d5b46c", width=5)
    draw.rounded_rectangle((17, 17, width - 18, height - 18), radius=10, outline="#597a86", width=2)
    cx, cy = width // 2, height // 2
    radius = min(width, height) // 4
    draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), outline="#d5b46c", width=4)
    draw.line((cx-radius, cy, cx+radius, cy), fill="#70c9c5", width=3)
    draw.line((cx, cy-radius, cx, cy+radius), fill="#70c9c5", width=3)
    draw.text((cx-8, cy-13), "✦", fill="#f0d997", font=_font(24, bold=True))
    if rotate_cross:
        back = back.rotate(90, expand=True)
    return back


def _number_badge(canvas: Image.Image, x: int, y: int, number: int):
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((x, y, x + 30, y + 30), fill="#071829", outline="#f0d997", width=2)
    text = str(number)
    font = _font(16, bold=True)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x + (30-(box[2]-box[0]))/2, y + (30-(box[3]-box[1]))/2-1), text, fill="#f0d997", font=font)


def render_spread_image(spread_key: str, draws: list[DrawnCard], deck_dir: Path | None = None, *, revealed_count: int | None = None) -> io.BytesIO:
    spread = SPREADS[spread_key]
    deck_dir = deck_dir or resolve_deck_dir()
    revealed = len(draws) if revealed_count is None else max(0, min(len(draws), revealed_count))
    if spread_key == "celtic_cross":
        width, height = 1160, 960
        canvas = Image.new("RGB", (width, height), "#061624")
        coordinates = ((385, 355), (350, 390), (385, 675), (105, 355), (385, 75), (665, 355),
                       (930, 700), (930, 490), (930, 280), (930, 70))
        card_size = (150, 225)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((8, 8, width-9, height-9), radius=22, outline="#d8b66a", width=4)
        draw.text((30, 22), spread.label, fill="#f0d997", font=_font(30, bold=True))
        draw.text((760, 29), f"Revealed {revealed}/{len(draws)}", fill="#b8cbd7", font=_font(18))
        for index, ((x, y), item) in enumerate(zip(coordinates, draws)):
            crossing = index == 1
            image = _card_face(item, deck_dir, card_size, rotate_cross=crossing) if index < revealed else _card_back(card_size, rotate_cross=crossing)
            canvas.paste(image, (x, y))
            _number_badge(canvas, x - 13, y - 13, index + 1)
    else:
        count = len(draws)
        card_w = 220 if count == 3 else 180
        card_h = int(card_w * 1.5)
        gap = 22
        margin = 34
        header = 84
        label_h = 48
        width = margin * 2 + count * card_w + (count - 1) * gap
        height = header + margin + card_h + label_h + margin
        canvas = Image.new("RGB", (width, height), "#061624")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((8, 8, width-9, height-9), radius=22, outline="#d8b66a", width=4)
        draw.text((28, 24), spread.label, fill="#f0d997", font=_font(28, bold=True))
        progress = f"Revealed {revealed}/{count}"
        pbox = draw.textbbox((0, 0), progress, font=_font(17))
        draw.text((width-pbox[2]-30, 31), progress, fill="#b8cbd7", font=_font(17))
        for index, item in enumerate(draws):
            x = margin + index * (card_w + gap)
            y = header + margin
            image = _card_face(item, deck_dir, (card_w, card_h)) if index < revealed else _card_back((card_w, card_h))
            canvas.paste(image, (x, y))
            draw.rectangle((x, y+card_h, x+card_w, y+card_h+label_h), fill="#10263d")
            label = f"{index+1}. {item.position}" if index < revealed else f"{index+1}. Unrevealed"
            draw.text((x+7, y+card_h+8), label[:25], fill="#f5df9d", font=_font(14, bold=True))
            if index < revealed:
                draw.text((x+7, y+card_h+27), item.orientation.title(), fill="#bdc9d8", font=_font(12))
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
    output.seek(0)
    return output


def render_single_card(item: DrawnCard, deck_dir: Path | None = None) -> io.BytesIO:
    deck_dir = deck_dir or resolve_deck_dir()
    image = _card_face(item, deck_dir, (512, 768))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    output.seek(0)
    return output


def tarot_intro(bot_name: str, question: str, preferences: TarotPreferences) -> str:
    line = f"**{bot_name}'s tarot table is open.** Choose a spread; cards will be revealed one at a time."
    if question.strip():
        line += f"\nYour question: *{question.strip()[:500]}*"
    return line + f"\nMeanings: **{preferences.meaning_mode.title()}** · Reversals: **{'On' if preferences.reversals else 'Off'}** · Detail: **{preferences.detail.title()}**"


def _private_interaction(interaction, preferences: TarotPreferences) -> bool:
    return preferences.visibility == "private" and getattr(interaction, "guild", None) is not None


AITextCallback = Callable[[str, int], Awaitable[str]]

def serialized_action(callback):
    @wraps(callback)
    async def wrapped(self, interaction, button):
        async with self._action_lock:
            if not await self.interaction_check(interaction):
                return
            if button.disabled:
                await interaction.response.send_message("That action is already complete.", ephemeral=True)
                return
            return await callback(self, interaction, button)
    return wrapped


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.message = None
        self._action_lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            try:
                await self.store.validate(getattr(self, "question", ""))
            except (PermissionError, ValueError):
                await interaction.response.send_message("This table expired or is unavailable. Open a new tarot reading.", ephemeral=True)
                return False
            return True
        await interaction.response.send_message("This tarot table belongs to someone else. Open your own reading.", ephemeral=True)
        return False

    def disable_all(self):
        for child in self.children:
            child.disabled = True

    async def on_error(self, interaction, error, item):
        # Never include exceptions, questions, provider payloads, or secrets.
        text = "The cards refused to settle. Open a new tarot table and try again."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def on_timeout(self):
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class TarotPreferencesView(OwnerView):
    def __init__(self, owner_id: int, bot_name: str, store: TarotStore, preferences: TarotPreferences):
        super().__init__(owner_id, timeout=300)
        self.bot_name, self.store, self.preferences = bot_name, store, preferences
        self._refresh_labels()

    def _refresh_labels(self):
        labels = (
            f"Reversals: {'On' if self.preferences.reversals else 'Off'}",
            f"Visibility: {self.preferences.visibility.title()}",
            f"Detail: {self.preferences.detail.title()}",
            f"Meanings: {self.preferences.meaning_mode.title()}",
        )
        for child, label in zip(self.children[:4], labels):
            child.label = label

    async def _set(self, interaction: discord.Interaction, key: str, value):
        self.preferences = await self.store.set_preference(self.owner_id, key, value)
        self._refresh_labels()
        await interaction.response.edit_message(content=preferences_text(self.bot_name, self.preferences), view=self)

    @discord.ui.button(label="Reversals", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    @serialized_action
    async def reversals_button(self, interaction, _button):
        await self._set(interaction, "reversals", not self.preferences.reversals)

    @discord.ui.button(label="Visibility", emoji="👁️", style=discord.ButtonStyle.secondary, row=0)
    @serialized_action
    async def visibility_button(self, interaction, _button):
        await self._set(interaction, "visibility", "private" if self.preferences.visibility == "public" else "public")

    @discord.ui.button(label="Detail", emoji="📖", style=discord.ButtonStyle.secondary, row=1)
    @serialized_action
    async def detail_button(self, interaction, _button):
        await self._set(interaction, "detail", "detailed" if self.preferences.detail == "brief" else "brief")

    @discord.ui.button(label="Meanings", emoji="🪶", style=discord.ButtonStyle.secondary, row=1)
    @serialized_action
    async def meanings_button(self, interaction, _button):
        await self._set(interaction, "meaning_mode", "standard" if self.preferences.meaning_mode == "lore" else "lore")

    @discord.ui.button(label="Done", emoji="✓", style=discord.ButtonStyle.success, row=2)
    @serialized_action
    async def done_button(self, interaction, _button):
        self.disable_all()
        await interaction.response.edit_message(content=preferences_text(self.bot_name, self.preferences) + "\n\nSettings saved.", view=self)


class TarotView(OwnerView):
    def __init__(self, owner_id: int, bot_name: str, ai_callback: AITextCallback, question: str, preferences: TarotPreferences, store: TarotStore):
        super().__init__(owner_id, timeout=180)
        self.bot_name, self.ai_callback = bot_name, ai_callback
        self.question = question.strip()[:500]
        self.preferences, self.store = preferences, store
        self._used = False
        self._lock = asyncio.Lock()

    async def _select(self, interaction: discord.Interaction, spread_key: str):
        async with self._lock:
            if self._used:
                await interaction.response.send_message("That deck has already been drawn.", ephemeral=True)
                return
            self._used = True
            self.disable_all()
        try:
            await interaction.response.edit_message(view=self)
            draws = draw_spread(spread_key, reversals=self.preferences.reversals)
            reveal = TarotRevealView(self.owner_id, self.bot_name, self.ai_callback, self.question, self.preferences, self.store, spread_key, draws)
            art = await asyncio.to_thread(render_spread_image, spread_key, draws, None, revealed_count=1)
            await self.store.validate()
            content = reveal.current_content()
            sent = await interaction.followup.send(
                content, file=discord.File(art, filename=f"{self.bot_name.lower()}-{spread_key}-reveal.jpg"),
                view=reveal, ephemeral=_private_interaction(interaction, self.preferences), wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            reveal.message = sent
        except Exception as error:
            print(f"[TAROT] open spread failed: {type(error).__name__}")
            await interaction.followup.send("The cards refused to settle. Try once more.", ephemeral=True)

    @discord.ui.button(label="1 · Celtic Cross", emoji="🌙", style=discord.ButtonStyle.primary, row=0)
    @serialized_action
    async def celtic_cross(self, interaction, _button): await self._select(interaction, "celtic_cross")

    @discord.ui.button(label="2 · Three-Card", emoji="✨", style=discord.ButtonStyle.success, row=0)
    @serialized_action
    async def three_card(self, interaction, _button): await self._select(interaction, "three_card")

    @discord.ui.button(label="3 · Five-Card Yes/No", emoji="⚖️", style=discord.ButtonStyle.secondary, row=0)
    @serialized_action
    async def yes_no(self, interaction, _button): await self._select(interaction, "yes_no")


class TarotRevealView(OwnerView):
    def __init__(self, owner_id: int, bot_name: str, ai_callback: AITextCallback, question: str, preferences: TarotPreferences, store: TarotStore, spread_key: str, draws: list[DrawnCard]):
        super().__init__(owner_id, timeout=300)
        self.bot_name, self.ai_callback, self.question = bot_name, ai_callback, question
        self.preferences, self.store, self.spread_key, self.draws = preferences, store, spread_key, draws
        self.revealed_count = 1
        self.children[0].label = f"Reveal card 2 of {len(draws)}"

    def current_content(self):
        item = self.draws[self.revealed_count - 1]
        return f"🔮 **{SPREADS[self.spread_key].label} · {self.revealed_count}/{len(self.draws)}**\n{reveal_reaction(self.bot_name, item, self.preferences)}"

    @discord.ui.button(label="Reveal next card", emoji="🃏", style=discord.ButtonStyle.primary)
    @serialized_action
    async def reveal_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.revealed_count += 1
        if self.revealed_count < len(self.draws):
            button.label = f"Reveal card {self.revealed_count + 1} of {len(self.draws)}"
            await interaction.response.defer()
            art = await asyncio.to_thread(render_spread_image, self.spread_key, self.draws, None, revealed_count=self.revealed_count)
            await self.store.validate()
            await interaction.edit_original_response(
                content=self.current_content(), attachments=[discord.File(art, filename="tarot-reveal.jpg")], view=self,
            )
            return
        self.disable_all()
        await interaction.response.defer()
        prompt = build_reading_prompt(self.bot_name, self.spread_key, self.draws, self.question, self.preferences)
        _, max_tokens = reading_limits(self.spread_key, self.preferences)
        try:
            reading = (await self.ai_callback(prompt, max_tokens)).strip()
        except Exception:
            reading = ""
        if not reading:
            reading = fallback_reading(self.spread_key, self.draws, self.preferences, question=self.question)
        result = TarotResultView(self.owner_id, self.bot_name, self.ai_callback, self.question, self.preferences, self.store, self.spread_key, self.draws, reading)
        art = await asyncio.to_thread(render_spread_image, self.spread_key, self.draws)
        await self.store.validate()
        pages = result.content_pages()
        message = await interaction.edit_original_response(
            content=pages[0], attachments=[discord.File(art, filename=f"{self.bot_name.lower()}-{self.spread_key}.jpg")], view=result,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        result.message = message
        for page in pages[1:]:
            await interaction.followup.send(
                page,
                ephemeral=_private_interaction(interaction, self.preferences),
                allowed_mentions=discord.AllowedMentions.none(),
            )


class ExplainCardModal(discord.ui.Modal, title="Explain a card"):
    card = discord.ui.TextInput(label="Card number or name", placeholder="Example: 2 or The Moon", max_length=80)

    def __init__(self, parent):
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction):
        if await self.parent_view.interaction_check(interaction):
            await self.parent_view.explain_card(interaction, str(self.card.value))


class FollowupModal(discord.ui.Modal, title="Ask about this reading"):
    question = discord.ui.TextInput(label="Follow-up question", style=discord.TextStyle.paragraph, max_length=400)

    def __init__(self, parent):
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction):
        if await self.parent_view.interaction_check(interaction):
            await self.parent_view.answer_followup(interaction, str(self.question.value))


class TarotResultView(OwnerView):
    def __init__(self, owner_id: int, bot_name: str, ai_callback: AITextCallback, question: str, preferences: TarotPreferences, store: TarotStore, spread_key: str, draws: list[DrawnCard], reading: str):
        super().__init__(owner_id, timeout=600)
        self.bot_name, self.ai_callback, self.question = bot_name, ai_callback, question
        self.preferences, self.store, self.spread_key = preferences, store, spread_key
        self.draws, self.reading = draws, reading
        self._clarifier_used = False
        self._saved = False

    def content_pages(self):
        if self.spread_key == "celtic_cross":
            return celtic_cross_pages(
                self.bot_name,
                self.question,
                self.draws,
                self.reading,
                self.preferences,
            )
        return self._single_content_pages()

    def content(self):
        return self.content_pages()[0]

    def _single_content_pages(self):
        heading = f"🔮 **{self.bot_name}'s {SPREADS[self.spread_key].label}**"
        if self.question:
            heading += f"\n*Question: {self.question}*"
        extra = ""
        if self.spread_key == "yes_no":
            result = yes_no_analysis(self.draws)
            extra = f"\n**Signal:** {result.positive} positive · {result.uncertain} uncertain · {result.warning} warning"
        fixed = f"{heading}\n\n{draw_summary(self.draws)}{extra}\n\n"
        continued = f"🔮 **{self.bot_name}'s {SPREADS[self.spread_key].label} (continued)**\n\n"
        return paginate_tarot_text(fixed, self.reading, continuation=continued)

    def _single_content(self):
        return self._single_content_pages()[0]

    async def _private_send(self, interaction, content, **kwargs):
        pages = paginate_tarot_text("", content, continuation="🔮 **Reading continued**\n\n")
        for index, page in enumerate(pages):
            await self.store.validate()
            options = dict(kwargs)
            if index:
                options.pop("file", None)
            options["ephemeral"] = _private_interaction(interaction, self.preferences)
            await interaction.followup.send(page, allowed_mentions=discord.AllowedMentions.none(), **options)

    @discord.ui.button(label="Draw a clarifier", emoji="🔍", style=discord.ButtonStyle.primary, row=0)
    @serialized_action
    async def clarifier_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._clarifier_used:
            await interaction.response.send_message("You already drew the clarifier for this reading.", ephemeral=True)
            return
        self._clarifier_used = True
        button.disabled = True
        await interaction.response.defer(thinking=True, ephemeral=_private_interaction(interaction, self.preferences))
        item = draw_one("Clarifier", reversals=self.preferences.reversals, exclude={draw.card.stem for draw in self.draws})
        prompt = build_clarifier_prompt(self.bot_name, self.question, self.draws, item, self.preferences)
        try:
            explanation = (await self.ai_callback(prompt, 320)).strip()
        except Exception:
            explanation = ""
        explanation = explanation or f"{item.meaning(self.preferences.meaning_mode).capitalize()}. This is the pressure point that clarifies the original pattern."
        art = await asyncio.to_thread(render_single_card, item)
        await self._private_send(interaction, f"🔍 **Clarifier: {item.card.name} ({item.orientation})**\n{explanation}", file=discord.File(art, filename="tarot-clarifier.jpg"))
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Explain a card", emoji="💬", style=discord.ButtonStyle.secondary, row=0)
    @serialized_action
    async def explain_button(self, interaction, _button):
        await interaction.response.send_modal(ExplainCardModal(self))

    @discord.ui.button(label="Ask a follow-up", emoji="❓", style=discord.ButtonStyle.secondary, row=0)
    @serialized_action
    async def followup_button(self, interaction, _button):
        await interaction.response.send_modal(FollowupModal(self))

    @discord.ui.button(label="Save reading", emoji="💾", style=discord.ButtonStyle.success, row=1)
    @serialized_action
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._saved:
            await interaction.response.send_message("This reading is already saved.", ephemeral=True)
            return
        reading_id = await self.store.save_reading(self.owner_id, self.bot_name, self.question, self.spread_key, self.draws, self.reading)
        self._saved = True
        button.disabled, button.label = True, "Saved"
        await interaction.response.send_message(f"Saved as tarot reading `#{reading_id}`. View it with `/tarothistory`.", ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    def _find_card(self, raw: str) -> DrawnCard | None:
        text = raw.strip().lower()
        if text.isdigit() and 1 <= int(text) <= len(self.draws):
            return self.draws[int(text)-1]
        return next((item for item in self.draws if text and text in item.card.name.lower()), None)

    async def explain_card(self, interaction: discord.Interaction, raw: str):
        item = self._find_card(raw)
        if item is None:
            await interaction.response.send_message(f"Choose a card number from 1 to {len(self.draws)}, or type its name.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=_private_interaction(interaction, self.preferences))
        try:
            answer = (await self.ai_callback(build_explain_prompt(self.bot_name, self.question, item, self.preferences), 320)).strip()
        except Exception:
            answer = ""
        answer = answer or item.meaning(self.preferences.meaning_mode).capitalize() + "."
        await self._private_send(interaction, f"💬 **{item.position}: {item.card.name} ({item.orientation})**\n{answer}")

    async def answer_followup(self, interaction: discord.Interaction, followup: str):
        try:
            await self.store.validate(followup)
        except (ValueError, PermissionError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.defer(thinking=True, ephemeral=_private_interaction(interaction, self.preferences))
        try:
            answer = (await self.ai_callback(build_followup_prompt(self.bot_name, self.question, followup, self.draws, self.reading, self.preferences), 360)).strip()
        except Exception:
            answer = ""
        answer = answer or "The existing pattern does not become clearer by forcing it. Re-read the outcome beside the obstacle."
        await self._private_send(interaction, f"❓ **Follow-up:** {followup[:400]}\n{answer}")


async def get_daily_card(store: TarotStore, user_id: int, bot_name: str, preferences: TarotPreferences) -> tuple[DrawnCard, str, str]:
    draw_date = datetime.now(timezone.utc).date().isoformat()
    row = await store.get_daily(user_id, bot_name, draw_date)
    if row is None:
        fresh = draw_one("Card of the day", reversals=preferences.reversals)
        row = await store.create_daily(user_id, bot_name, draw_date, fresh)
    card = CARD_BY_STEM[row["card_stem"]]
    item = DrawnCard(card, "Card of the day", bool(row["reversed"]))
    return item, row.get("reading", ""), draw_date
