"""
╔══════════════════════════════════════════════════════════════════╗
║  CASSIAN — DATABASE MODELS                                       ║
║  The complete data model for the book pipeline web app.          ║
║                                                                  ║
║  Uses SQLAlchemy ORM with SQLite.                                ║
║  One file on disk — no database server needed.                   ║
║                                                                  ║
║  Entities:                                                       ║
║    User            — authenticated user (Google OAuth)           ║
║    Project         — a book being processed                      ║
║    OutputProfile   — publisher + format specs (Lulu, KDP, etc.)  ║
║    PipelineRun     — one full (or partial) pipeline execution    ║
║    AgentRun        — one agent's run within a PipelineRun        ║
║    Chapter         — a chapter's paths and state                 ║
║    Illustration    — per-chapter image: provider, placement,     ║
║                      style, approval workflow                    ║
║    IllustrationStyle — saved style profiles for reuse            ║
║    Edit            — a single suggested text change, accept/rej  ║
║    Cover           — wraparound cover file(s) per output profile ║
║    Output          — final files (PDFs, reports, covers)         ║
║    Snapshot        — timestamped backup before each agent run    ║
║    WorldRule       — project bible: characters, rules, terms     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean,
    DateTime, Enum, ForeignKey, JSON
)
from sqlalchemy.orm import relationship, DeclarativeBase


# ─────────────────────────────────────────────────────────────────
#  BASE
# ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────────────────────────

class ProjectStatus(str, enum.Enum):
    DRAFT    = "draft"      # created, no manuscript uploaded yet
    ACTIVE   = "active"     # manuscript uploaded, work in progress
    COMPLETE = "complete"   # final PDF produced and approved
    ARCHIVED = "archived"   # finished, read-only


class RunStatus(str, enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"    # waiting for user input
    COMPLETE  = "complete"
    FAILED    = "failed"


class AgentStatus(str, enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"    # waiting for user approval (illustrations, edits)
    COMPLETE  = "complete"
    FAILED    = "failed"
    SKIPPED   = "skipped"   # user chose not to run this agent


class IllustrationStatus(str, enum.Enum):
    PENDING      = "pending"
    GENERATING   = "generating"
    GENERATED    = "generated"    # image exists, awaiting approval
    APPROVED     = "approved"
    REJECTED     = "rejected"     # needs regeneration
    REGENERATING = "regenerating"
    SKIPPED      = "skipped"


class EditStatus(str, enum.Enum):
    AUTO_ACCEPTED = "auto_accepted"  # Tier 1 or below sensitivity threshold
    PENDING       = "pending"        # awaiting user review
    APPROVED      = "approved"
    REJECTED      = "rejected"       # original text kept


class EditTier(str, enum.Enum):
    TIER_1 = "tier_1"   # automatic fix — auto-accepted
    TIER_2 = "tier_2"   # AI prose suggestion — user reviews
    TIER_3 = "tier_3"   # structural/flagged — always manual


class CoverStatus(str, enum.Enum):
    PENDING   = "pending"
    GENERATING = "generating"
    COMPLETE  = "complete"
    APPROVED  = "approved"
    FAILED    = "failed"


# ─── Illustration enums ───────────────────────────────────────────

class IllustrationProvider(str, enum.Enum):
    """Which AI model generates the image."""
    IMAGEN3          = "imagen3"           # Google Imagen 3 via Vertex AI (current default)
    GROK_FLUX        = "grok_flux"         # xAI Grok image generator (FLUX-based, best style ref)
    FLUX_REPLICATE   = "flux_replicate"    # FLUX via Replicate API
    DALLE3           = "dalle3"            # OpenAI DALL-E 3
    STABLE_DIFFUSION = "stable_diffusion"  # SD via API (most flexible, slowest)
    GEMINI_IMAGE     = "gemini_image"      # Gemini image generation (flash)


class StyleApproach(str, enum.Enum):
    """How style is communicated to the image model."""
    TEXT_ONLY       = "text_only"       # style described entirely in the text prompt
    REFERENCE_IMAGE = "reference_image" # a reference image uploaded to guide style
    BOTH            = "both"            # text prompt + reference image together


class PlacementType(str, enum.Enum):
    """Where in the chapter/page the illustration appears."""
    CHAPTER_HEADER  = "chapter_header"   # full-width image at the top of the chapter
    INLINE          = "inline"           # floated next to a specific paragraph
    TITLE_PAGE      = "title_page"       # only on the book's title page, not chapters
    RANDOM          = "random"           # agent picks the strongest visual moment
    NONE            = "none"             # no illustration for this chapter


class EdgeStyle(str, enum.Enum):
    """Visual treatment of the illustration's edges."""
    STRAIGHT  = "straight"    # clean rectangular image, no edge effect
    CURVED    = "curved"      # rounded corners
    TATTERED  = "tattered"    # rough torn-paper edge
    VIGNETTE  = "vignette"    # fades to transparent at the edges
    TORN      = "torn"        # irregular torn edge, more dramatic than tattered
    CIRCULAR  = "circular"    # circular crop


# ─── Output format enums ─────────────────────────────────────────

class Publisher(str, enum.Enum):
    LULU          = "lulu"
    INGRAM_SPARK  = "ingram_spark"
    KDP           = "kdp"           # Amazon Kindle Direct Publishing
    DRAFT2DIGITAL = "draft2digital"
    GENERIC       = "generic"       # custom / no publisher specs


class BookFormat(str, enum.Enum):
    HARDCOVER_CASEWRAP  = "hardcover_casewrap"   # Lulu's standard hardcover
    HARDCOVER_DUSTJACKET= "hardcover_dustjacket" # hardcover with removable jacket
    TRADE_PAPERBACK     = "trade_paperback"       # 6×9 softcover
    MASS_MARKET_PB      = "mass_market_pb"        # 4.25×6.87 softcover
    EBOOK_EPUB          = "ebook_epub"            # reflowable ebook (no fixed layout)


class CoverType(str, enum.Enum):
    WRAPAROUND   = "wraparound"   # front + spine + back as one wide file (Lulu default)
    DUST_JACKET  = "dust_jacket"  # separate front/back panels + flaps
    FRONT_ONLY   = "front_only"   # ebook cover, just a front image


# ─────────────────────────────────────────────────────────────────
#  PROJECT
#  One row per book.
# ─────────────────────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)  # nullable for migration
    name        = Column(String(255), nullable=False)
    author      = Column(String(255), nullable=False)
    description = Column(Text, default="")
    status      = Column(Enum(ProjectStatus), default=ProjectStatus.DRAFT, nullable=False)

    # Where the uploaded manuscript .docx files live on disk
    manuscript_dir  = Column(String(512), nullable=True)
    chapter_count   = Column(Integer, default=0)

    # How sections are treated in the layout agent
    # "novel"   → "Chapter X" headings, sections start on right-hand pages
    # "poetry"  → section title only (no "Chapter X"), natural page flow,
    #             poem title appears as running header on overflow pages
    # "essays"  → essay title only, similar to poetry but wider spacing
    layout_mode   = Column(String(32), default="novel")

    # User-defined reading order — list of filenames in sequence.
    # e.g. ["Prologue.docx", "The Burning River.docx", "chapter 4.docx"]
    # Runner prefixes these as 01_, 02_, 03_… before passing to the pipeline.
    # Any uploaded file NOT in this list is appended at the end alphabetically.
    chapter_order = Column(JSON, default=list)

    created_at  = Column(DateTime, default=datetime.now, nullable=False)
    updated_at  = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Genre — drives World Rules templates and agent behavior
    genre       = Column(String(64), default="fiction")
    # "fiction", "sci-fi", "poetry", "cookbook", "children", "business",
    # "memoir", "fantasy", "romance", "thriller", "nonfiction", "other"

    # Relationships
    owner           = relationship("User",          back_populates="projects")
    runs            = relationship("PipelineRun",  back_populates="project",
                                   cascade="all, delete-orphan")
    chapters        = relationship("Chapter",      back_populates="project",
                                   cascade="all, delete-orphan")
    output_profiles = relationship("OutputProfile", back_populates="project",
                                   cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Project id={self.id} name='{self.name}' status={self.status}>"


# ─────────────────────────────────────────────────────────────────
#  OUTPUT PROFILE
#  One row per publisher+format combination for a project.
#  A project can have multiple profiles — e.g. Lulu hardcover AND
#  KDP trade paperback. Each pipeline run targets one profile.
#
#  The profile drives:
#    - Page dimensions and margins  →  Layout agent
#    - Spine width formula          →  Cover agent
#    - Bleed requirements           →  Cover agent
#    - DPI requirements             →  Illustration agent
# ─────────────────────────────────────────────────────────────────

class OutputProfile(Base):
    __tablename__ = "output_profiles"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    project_id  = Column(Integer, ForeignKey("projects.id"), nullable=False)

    name        = Column(String(255), nullable=False)  # e.g. "Lulu 6×9 Hardcover"
    publisher   = Column(Enum(Publisher),   default=Publisher.LULU,    nullable=False)
    book_format = Column(Enum(BookFormat),  default=BookFormat.HARDCOVER_CASEWRAP, nullable=False)
    cover_type  = Column(Enum(CoverType),   default=CoverType.WRAPAROUND, nullable=False)

    # Interior page dimensions
    trim_width_inches   = Column(Float, default=6.0)
    trim_height_inches  = Column(Float, default=9.0)

    # Interior margins
    margin_top_inches     = Column(Float, default=1.0)
    margin_bottom_inches  = Column(Float, default=1.0)
    margin_inside_inches  = Column(Float, default=1.25)  # gutter
    margin_outside_inches = Column(Float, default=0.75)

    # Bleed (extra area outside trim, for full-bleed covers)
    bleed_inches = Column(Float, default=0.125)

    # Resolution requirement for print
    dpi = Column(Integer, default=300)

    # Paper type affects spine width calculation
    # "cream" paper is slightly thicker than "white"
    paper_type = Column(String(32), default="cream")

    # Spine width is calculated as: page_count × paper_thickness_per_page
    # Each publisher has slightly different values — stored here as a JSON dict
    # e.g. { "white_per_page": 0.002252, "cream_per_page": 0.0025,
    #         "cover_boards": 0.05, "min_spine_width": 0.25 }
    spine_formula = Column(JSON, default=dict)

    # Whether this is the default profile for the project
    is_default = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.now, nullable=False)

    # Relationships
    project      = relationship("Project",      back_populates="output_profiles")
    pipeline_runs = relationship("PipelineRun", back_populates="output_profile")
    covers       = relationship("Cover",        back_populates="output_profile",
                                cascade="all, delete-orphan")

    def __repr__(self):
        return f"<OutputProfile '{self.name}' {self.publisher}/{self.book_format}>"


# ─────────────────────────────────────────────────────────────────
#  PIPELINE RUN
#  One execution of the pipeline (full or partial).
#  Always targets one OutputProfile.
# ─────────────────────────────────────────────────────────────────

class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    project_id        = Column(Integer, ForeignKey("projects.id"),        nullable=False)
    output_profile_id = Column(Integer, ForeignKey("output_profiles.id"), nullable=True)

    name    = Column(String(255), default="Pipeline Run")
    status  = Column(Enum(RunStatus), default=RunStatus.PENDING, nullable=False)

    # Which agents to run — list of ints, e.g. [1, 2, 3, 5]
    # Agents: 1=ingestion, 2=consistency, 3=editing, 4=illustration,
    #         5=layout, 6=cover, 7=qc
    agents_selected = Column(JSON, default=lambda: [1, 2, 3, 4, 5, 6, 7])

    # Which agent is currently active
    current_agent = Column(Integer, nullable=True)

    # Per-agent config snapshot — keyed by agent number (as string for JSON)
    # e.g. {
    #   "3": { "creativity_level": 3, "sensitivity": 0.6 },
    #   "4": { "provider": "grok_flux", "illustrations_per_chapter": 1 }
    # }
    agent_config = Column(JSON, default=dict)

    # Edit sensitivity threshold (0.0–1.0)
    # Changes with AI confidence BELOW this value are auto-accepted.
    # Changes ABOVE it are held for manual review.
    # 0.0 = review everything; 1.0 = auto-accept everything
    edit_sensitivity = Column(Float, default=0.5)

    # Page count — populated after Agent 5 (layout) completes
    # Required by Agent 6 (cover) to calculate spine width
    page_count = Column(Integer, nullable=True)

    started_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.now, nullable=False)

    # Relationships
    project        = relationship("Project",       back_populates="runs")
    output_profile = relationship("OutputProfile", back_populates="pipeline_runs")
    agent_runs     = relationship("AgentRun",      back_populates="pipeline_run",
                                  cascade="all, delete-orphan")
    illustrations  = relationship("Illustration",  back_populates="pipeline_run",
                                  cascade="all, delete-orphan")
    edits          = relationship("Edit",          back_populates="pipeline_run",
                                  cascade="all, delete-orphan")
    covers         = relationship("Cover",         back_populates="pipeline_run",
                                  cascade="all, delete-orphan")
    outputs        = relationship("Output",        back_populates="pipeline_run",
                                  cascade="all, delete-orphan")

    def __repr__(self):
        return f"<PipelineRun id={self.id} project={self.project_id} status={self.status}>"


# ─────────────────────────────────────────────────────────────────
#  AGENT RUN
#  One row per agent per pipeline run.
# ─────────────────────────────────────────────────────────────────

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False)

    agent_num   = Column(Integer,    nullable=False)
    agent_name  = Column(String(64), nullable=False)
    # agent_name values: ingestion | consistency | editing | illustration |
    #                    layout | cover | qc
    status      = Column(Enum(AgentStatus), default=AgentStatus.PENDING, nullable=False)

    input_dir   = Column(String(512), nullable=True)
    output_dir  = Column(String(512), nullable=True)
    log_path    = Column(String(512), nullable=True)

    # Config snapshot at run time
    settings    = Column(JSON, default=dict)

    # Stats after completion
    # e.g. { "chapters_processed": 31, "edits_made": 142, "errors": 0 }
    summary     = Column(JSON, default=dict)

    started_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    pipeline_run = relationship("PipelineRun", back_populates="agent_runs")

    def __repr__(self):
        return f"<AgentRun agent={self.agent_num}:{self.agent_name} status={self.status}>"


# ─────────────────────────────────────────────────────────────────
#  CHAPTER
#  One row per chapter per project.
# ─────────────────────────────────────────────────────────────────

class Chapter(Base):
    __tablename__ = "chapters"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    project_id  = Column(Integer, ForeignKey("projects.id"), nullable=False)

    chapter_key    = Column(String(32),  nullable=False)   # "01" … "30", "epilogue"
    chapter_number = Column(Integer,     nullable=True)     # sort order; null for epilogue
    title          = Column(String(512), default="")        # heading from manuscript
    chapter_name   = Column(String(512), default="")        # evocative name (Agent 5a)
    word_count     = Column(Integer,     default=0)

    # File paths at each stage
    original_path  = Column(String(512), nullable=True)    # .docx
    ingested_path  = Column(String(512), nullable=True)    # .json (Agent 1)
    edited_path    = Column(String(512), nullable=True)    # .json (Agent 3)

    # Per-chapter illustration settings
    # Overrides the run-level defaults for this chapter specifically
    illustrations_count = Column(Integer, nullable=True)
    # None = use run default; 0 = no illustration; N = generate N images

    project      = relationship("Project",      back_populates="chapters")
    illustrations = relationship("Illustration", back_populates="chapter",
                                 cascade="all, delete-orphan")
    edits        = relationship("Edit",         back_populates="chapter",
                                cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Chapter key={self.chapter_key} words={self.word_count}>"


# ─────────────────────────────────────────────────────────────────
#  ILLUSTRATION
#  One row per image per chapter per run.
#  Multiple rows for the same chapter = multiple illustrations.
#
#  Tracks the full cycle:
#    provider selection → scene analysis → prompt generation →
#    image generation → edge/style post-processing → approval →
#    (reject → regenerate) → final TIFF
# ─────────────────────────────────────────────────────────────────

class Illustration(Base):
    __tablename__ = "illustrations"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    chapter_id      = Column(Integer, ForeignKey("chapters.id"),      nullable=False)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False)

    status = Column(Enum(IllustrationStatus),
                    default=IllustrationStatus.PENDING, nullable=False)

    # ── Generation config ──────────────────────────────────────────────────────

    provider        = Column(Enum(IllustrationProvider),
                             default=IllustrationProvider.IMAGEN3, nullable=False)
    style_approach  = Column(Enum(StyleApproach),
                             default=StyleApproach.TEXT_ONLY, nullable=False)

    # Path to the style reference image (if style_approach includes reference)
    style_reference_path = Column(String(512), nullable=True)

    # Model-specific parameters used (seed, guidance_scale, etc.)
    # Stored so a generation can be exactly reproduced if needed
    generation_params = Column(JSON, default=dict)

    # ── Placement ──────────────────────────────────────────────────────────────

    placement_type      = Column(Enum(PlacementType),
                                 default=PlacementType.CHAPTER_HEADER, nullable=False)
    placement_paragraph = Column(Integer, nullable=True)
    # For INLINE placement: index of the paragraph to float the image beside.
    # None for all other placement types.

    # ── Visual style / edge treatment ─────────────────────────────────────────

    edge_style    = Column(Enum(EdgeStyle), default=EdgeStyle.STRAIGHT, nullable=False)
    is_full_bleed = Column(Boolean, default=False)
    # Additional post-processing options (drop shadow, border width, opacity, etc.)
    style_options = Column(JSON, default=dict)

    # ── Content ───────────────────────────────────────────────────────────────

    # Scene analysis + prompt from Gemini
    prompt_data = Column(JSON, default=dict)
    # Contains: scene description, image prompt text, mood tags,
    #           negative terms, palette guidance, character notes

    # ── File paths ────────────────────────────────────────────────────────────

    raw_image_path  = Column(String(512), nullable=True)
    # The original generated image (PNG/JPG) before post-processing

    processed_path  = Column(String(512), nullable=True)
    # After edge treatment and Pillow post-processing

    final_path      = Column(String(512), nullable=True)
    # Final CMYK TIFF at 300 DPI, ready for layout

    thumbnail_path  = Column(String(512), nullable=True)
    # JPEG thumbnail for display in the web approval UI

    # ── Approval workflow ─────────────────────────────────────────────────────

    attempts        = Column(Integer, default=0)
    reviewed_at     = Column(DateTime, nullable=True)
    rejection_note  = Column(Text, default="")
    # User's reason for rejecting — helps guide regeneration prompt

    chapter      = relationship("Chapter",     back_populates="illustrations")
    pipeline_run = relationship("PipelineRun", back_populates="illustrations")

    def __repr__(self):
        return (f"<Illustration ch={self.chapter_id} "
                f"provider={self.provider} status={self.status} "
                f"placement={self.placement_type} edge={self.edge_style}>")


# ─────────────────────────────────────────────────────────────────
#  EDIT
#  One row per suggested text change from Agent 2 or 3.
# ─────────────────────────────────────────────────────────────────

class Edit(Base):
    __tablename__ = "edits"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    chapter_id      = Column(Integer, ForeignKey("chapters.id"),      nullable=False)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False)

    agent_num  = Column(Integer,       nullable=False)   # 2 or 3
    tier       = Column(Enum(EditTier), nullable=False)
    edit_type  = Column(String(128),   default="")
    # e.g. "name_correction", "prose_polish", "blindness_fix", "continuity_fix"

    status = Column(Enum(EditStatus), default=EditStatus.PENDING, nullable=False)

    original_text  = Column(Text, nullable=False)
    suggested_text = Column(Text, nullable=False)

    # Context shown in the review UI so the change makes sense in isolation
    context_before = Column(Text, default="")
    context_after  = Column(Text, default="")

    paragraph_index = Column(Integer, nullable=True)
    char_offset     = Column(Integer, nullable=True)

    # AI confidence score (0.0–1.0)
    # Compared against the run's edit_sensitivity threshold at review time.
    # Below threshold → auto-accepted. Above → queued for manual review.
    confidence = Column(Float, default=1.0)

    reviewed_at = Column(DateTime, nullable=True)

    chapter      = relationship("Chapter",     back_populates="edits")
    pipeline_run = relationship("PipelineRun", back_populates="edits")

    def __repr__(self):
        return (f"<Edit agent={self.agent_num} tier={self.tier} "
                f"type='{self.edit_type}' status={self.status}>")


# ─────────────────────────────────────────────────────────────────
#  COVER
#  One row per pipeline run per output profile.
#  Handles the full wraparound cover workflow.
#
#  Dependency: spine_width_inches cannot be calculated until
#  the pipeline run's page_count is set by Agent 5 (layout).
#  Agent 6 (cover) reads page_count, applies the profile's
#  spine_formula, and writes spine_width_inches here.
#
#  Lulu wraparound = back cover + spine + front cover
#  as one wide image file at 300 DPI with bleed on all sides.
# ─────────────────────────────────────────────────────────────────

class Cover(Base):
    __tablename__ = "covers"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    project_id        = Column(Integer, ForeignKey("projects.id"),        nullable=False)
    pipeline_run_id   = Column(Integer, ForeignKey("pipeline_runs.id"),   nullable=False)
    output_profile_id = Column(Integer, ForeignKey("output_profiles.id"), nullable=False)

    status = Column(Enum(CoverStatus), default=CoverStatus.PENDING, nullable=False)

    # ── Spine calculation ──────────────────────────────────────────────────────

    page_count_used    = Column(Integer, nullable=True)
    # Snapshot of the page count from layout — used to calculate spine width.

    spine_width_inches = Column(Float, nullable=True)
    # Calculated by Agent 6 using: page_count × paper_thickness + cover_boards
    # Cannot be known until Agent 5 completes.

    # ── Content ───────────────────────────────────────────────────────────────

    back_cover_text   = Column(Text, default="")
    # The blurb / synopsis shown on the back cover.

    front_prompt_data = Column(JSON, default=dict)
    # Scene/style prompt used to generate the front cover illustration.

    back_prompt_data  = Column(JSON, default=dict)
    # Prompt used for back cover art (if any — sometimes just solid color + text).

    # ── File paths ────────────────────────────────────────────────────────────

    front_image_path    = Column(String(512), nullable=True)
    back_image_path     = Column(String(512), nullable=True)
    spine_image_path    = Column(String(512), nullable=True)
    combined_path       = Column(String(512), nullable=True)
    # The single wraparound file Lulu/IngramSpark requires for upload.

    thumbnail_path      = Column(String(512), nullable=True)
    # Preview image of the full wraparound for the web UI.

    # ── Provider (same options as interior illustrations) ─────────────────────

    provider       = Column(Enum(IllustrationProvider),
                            default=IllustrationProvider.IMAGEN3, nullable=False)
    style_approach = Column(Enum(StyleApproach),
                            default=StyleApproach.TEXT_ONLY, nullable=False)
    style_reference_path = Column(String(512), nullable=True)

    # ── Approval ──────────────────────────────────────────────────────────────

    attempts      = Column(Integer, default=0)
    reviewed_at   = Column(DateTime, nullable=True)
    rejection_note = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.now, nullable=False)

    project        = relationship("Project")
    pipeline_run   = relationship("PipelineRun",   back_populates="covers")
    output_profile = relationship("OutputProfile",  back_populates="covers")

    def __repr__(self):
        return (f"<Cover run={self.pipeline_run_id} "
                f"profile='{self.output_profile_id}' "
                f"spine={self.spine_width_inches}\" status={self.status}>")


# ─────────────────────────────────────────────────────────────────
#  OUTPUT
#  One row per deliverable file produced by a pipeline run.
# ─────────────────────────────────────────────────────────────────

class Output(Base):
    __tablename__ = "outputs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False)

    output_type = Column(String(64), nullable=False)
    # e.g. "pdf_interior", "pdf_cover_wraparound", "consistency_report",
    #      "layout_report", "editing_changelog", "illustration_manifest"

    file_path       = Column(String(512), nullable=False)
    file_size_bytes = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.now, nullable=False)
    notes           = Column(Text, default="")

    pipeline_run = relationship("PipelineRun", back_populates="outputs")

    def __repr__(self):
        return f"<Output type='{self.output_type}' size={self.file_size_bytes}>"


# ─────────────────────────────────────────────────────────────────
#  USER
#  Authenticated via Google OAuth. Each user owns their own
#  projects. In single-user mode, a default user is auto-created.
# ─────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    OWNER  = "owner"     # full access, can manage billing
    EDITOR = "editor"    # can run agents and edit projects
    VIEWER = "viewer"    # read-only access


class User(Base):
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    email       = Column(String(255), unique=True, nullable=False)
    name        = Column(String(255), default="")
    avatar_url  = Column(String(512), nullable=True)   # Google profile picture

    # Auth fields
    google_id     = Column(String(128), unique=True, nullable=True)   # set when user signs in via Google
    password_hash = Column(String(255), nullable=True)                # set when user registers with email/password
    is_admin      = Column(Boolean, default=False)                    # system admin (Elliot only)

    role        = Column(Enum(UserRole), default=UserRole.OWNER, nullable=False)
    is_active   = Column(Boolean, default=True)

    # Billing stub — plan and usage tracking
    plan        = Column(String(64), default="free")    # "free", "pro", "enterprise"
    runs_used   = Column(Integer, default=0)
    runs_limit  = Column(Integer, default=100)          # per billing cycle

    created_at  = Column(DateTime, default=datetime.now, nullable=False)
    last_login  = Column(DateTime, nullable=True)

    # Relationships
    projects = relationship("Project", back_populates="owner")

    def __repr__(self):
        return f"<User id={self.id} email='{self.email}' role={self.role}>"


# ─────────────────────────────────────────────────────────────────
#  SNAPSHOT
#  Automatic timestamped backup of the current manuscript state
#  taken before any agent modifies it.
#
#  The Source snapshot (agent_name="source") is created at intake
#  and is NEVER overwritten — it's the immutable original.
# ─────────────────────────────────────────────────────────────────

class Snapshot(Base):
    __tablename__ = "snapshots"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    project_id  = Column(Integer, ForeignKey("projects.id"), nullable=False)

    # Which agent was about to run when this snapshot was taken
    agent_name  = Column(String(64), nullable=False)
    # "source" = original intake (immutable)
    # "dev_editor", "copy_editor", "illustration", etc.

    # Path to the snapshot directory on disk
    # e.g. projects/{id}/snapshots/20260305_143022_dev_editor/
    snapshot_dir = Column(String(512), nullable=False)

    # Human-readable label for the timeline view
    label       = Column(String(255), default="")

    # Size on disk in bytes (for storage monitoring)
    size_bytes  = Column(Integer, default=0)

    created_at  = Column(DateTime, default=datetime.now, nullable=False)

    project = relationship("Project", backref="snapshots")

    def __repr__(self):
        return f"<Snapshot project={self.project_id} agent='{self.agent_name}' at={self.created_at}>"


# ─────────────────────────────────────────────────────────────────
#  WORLD RULE
#  One entry in the project's "bible" — a known fact, character
#  detail, timeline event, terminology definition, or constraint.
#
#  Categories: character, location, timeline, rule, terminology,
#              style_decision, genre_default
# ─────────────────────────────────────────────────────────────────

class WorldRule(Base):
    __tablename__ = "world_rules"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    project_id  = Column(Integer, ForeignKey("projects.id"), nullable=False)

    category    = Column(String(64), nullable=False)
    # "character", "location", "timeline", "rule", "terminology",
    # "style_decision", "genre_default"

    title       = Column(String(255), nullable=False)
    # e.g. "Daniil Tsarasov", "City 40 Location", "No Tomatoes in Recipes"

    content     = Column(Text, nullable=False)
    # Full description / detail of the rule

    # Optional structured data for programmatic use by agents
    rule_data   = Column(JSON, default=dict)
    # e.g. for a character: {"born": 1928, "aliases": ["Karpov"], "blind": true}

    # Where this rule came from
    source      = Column(String(64), default="manual")
    # "manual" = user added it, "consistency_editor" = auto-detected,
    # "genre_default" = pre-populated from genre template, "intake" = auto-scan

    # Ordering within category
    sort_order  = Column(Integer, default=0)

    is_active   = Column(Boolean, default=True)   # soft-delete
    created_at  = Column(DateTime, default=datetime.now, nullable=False)
    updated_at  = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    project = relationship("Project", backref="world_rules")

    def __repr__(self):
        return f"<WorldRule '{self.title}' cat={self.category} project={self.project_id}>"


# ─────────────────────────────────────────────────────────────────
#  ILLUSTRATION STYLE
#  Saved style profiles from the Illustration Architect's
#  Style Ledger. Replaces localStorage with database persistence.
# ─────────────────────────────────────────────────────────────────

class IllustrationStyle(Base):
    __tablename__ = "illustration_styles"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    project_id  = Column(Integer, ForeignKey("projects.id"), nullable=False)

    name        = Column(String(255), nullable=False)
    # e.g. "Epic Painterly", "Minimal Ink", "Watercolor Botanicals"

    style_input = Column(Text, default="")
    # The original style instruction (artist name, description, etc.)

    # Full style profile as generated by the Stylist sub-agent
    style_profile = Column(JSON, default=dict)
    # Contains: medium, palette, lighting, perspective, texture, mood

    container_mask  = Column(String(256), default="straight")
    # Edge treatment: preset (straight, curved, etc.) or custom description

    # Path to a reference image used to derive this style
    style_reference_path = Column(String(512), nullable=True)

    is_default  = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.now, nullable=False)

    project = relationship("Project", backref="illustration_styles")

    def __repr__(self):
        return f"<IllustrationStyle '{self.name}' project={self.project_id}>"
