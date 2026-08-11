---
name: Telegram Turntable
description: A private, focused music player for audio stored in Telegram.
colors:
  paper-light: "#faf9f6"
  rail-light: "#f2f0ea"
  surface-light: "#ffffff"
  ink-light: "#14110f"
  graphite-light: "#6b655d"
  rule-light: "#ddd8ce"
  rule-soft-light: "#ebe7df"
  charred-paper: "#12100e"
  blackened-rail: "#080706"
  warm-graphite: "#1b1815"
  old-ivory: "#f2efe8"
  ash: "#a39c92"
  rule-dark: "#332e29"
  rule-soft-dark: "#24211d"
  vinyl-red: "#d4574a"
  vinyl-red-light: "#8c2f24"
  danger: "#e0796a"
  danger-light: "#a2331f"
  ok: "#6fbf8a"
  ok-light: "#2f6b45"
typography:
  display:
    fontFamily: "Archivo, Avenir, Segoe UI, sans-serif"
    fontSize: "clamp(32px, 5vw, 44px)"
    fontWeight: 500
    lineHeight: 0.98
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "Archivo, Avenir, Segoe UI, sans-serif"
    fontSize: "28px"
    fontWeight: 520
    lineHeight: 1.1
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Archivo, Avenir, Segoe UI, sans-serif"
    fontSize: "22px"
    fontWeight: 550
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  body:
    fontFamily: "Archivo, Avenir, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "IBM Plex Mono, SFMono-Regular, monospace"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "0.085em"
rounded:
  sm: "4px"
  md: "6px"
  lg: "12px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "18px"
  xl: "24px"
  2xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.ink-light}"
    textColor: "{colors.paper-light}"
    rounded: "{rounded.md}"
    padding: "0 15px"
    height: "40px"
  button-secondary:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.ink-light}"
    rounded: "{rounded.md}"
    padding: "0 15px"
    height: "40px"
  input:
    backgroundColor: "{colors.paper-light}"
    textColor: "{colors.ink-light}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "42px"
  source-row:
    backgroundColor: "{colors.rail-light}"
    textColor: "{colors.ink-light}"
    rounded: "{rounded.md}"
    padding: "0 9px"
    height: "54px"
  player:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.ink-light}"
    padding: "0 24px"
    height: "96px"
---

# Design System: Telegram Turntable

## 1. Overview

**Creative North Star: "The Record Crate"**

Telegram Turntable is a private listening instrument for a personal collection that happens to live in Telegram. The system is bold, chic, and focused: editorial typography, restrained surfaces, deliberate density, and a single record-red signal give the interface confidence without competing with the music or artwork.

The visual language treats the library as a curated crate rather than a generic streaming catalog. Source context remains close to the track, controls stay available without shouting, and depth is used to explain the relationship between the library, Now Playing, menus, and the persistent player. It explicitly rejects generic streaming-service sameness, dashboard-like clutter, playful consumer-app decoration, neon music-player clichés, and opaque Telegram utility chrome.

**Key Characteristics:**
- Editorial, condensed Archivo headlines with precise mono labels.
- Warm tinted neutrals in both light and dark themes.
- Restrained palette with Vinyl Red reserved for playback and meaningful status.
- Dense, scan-friendly library rows with generous separation between functional regions.
- Quiet layered elevation for panels, menus, and the persistent player.

## 1.1 Redesign contract

The redesign keeps the Record Crate identity while making the relationship between moving content and a small number of physical interface layers explicit.

### Product mode

Operate / music-library application.

### Core identity

- Warm paper
- Smoked glass
- Album artwork
- Catalogue typography
- Vinyl-red playback indication
- Subtle grain

### Signature effect

Content physically scrolls beneath a 36px progressive glass layer whose falloff extends 48px beyond the reserved header space. Glass belongs where a surface overlays moving content, not on every component.

### Blur scale

- `0px` = normal content
- `20px` = secondary glass
- `36px` = signature glass

### Accent

Vinyl red only. Use it for meaningful playback or status indication, not decoration.

### Typography

- **Archivo** = UI and display typography
- **IBM Plex Mono** = metadata and catalogue/data typography

Do not introduce another font for the redesign.

### Motion

Motion is restrained and purposeful. Preserve existing interaction choreography and always respect `prefers-reduced-motion`.

### Additional anti-patterns

- No glass on every component
- No purple/blue AI gradients
- No random glow
- No decorative waveform spam
- No arbitrary new fonts

### Glass decision rule

Before adding a glass background to a new button or component, ask whether it overlays moving content or represents a deliberate physical layer. Usually the answer is no. Normal controls should generally use a transparent or quiet surface treatment.

## 2. Colors

The palette is a restrained warm-neutral field: old paper and ivory in light mode, charred paper and warm graphite in dark mode, with Vinyl Red acting as the occasional physical mark of playback.

### Primary
- **Vinyl Red** (#d4574a): Playback progress, current-track marks, meaningful active states, and selected music signals in dark mode.
- **Vinyl Red Light** (#8c2f24): The same signal in light mode, tuned for contrast against paper.

### Neutral
- **Charred Paper** (#12100e): Dark-theme page canvas.
- **Blackened Rail** (#080706): Dark-theme source navigation rail.
- **Warm Graphite** (#1b1815): Dark-theme raised surfaces and control backgrounds.
- **Old Ivory** (#f2efe8): Dark-theme primary text and high-emphasis controls.
- **Ash** (#a39c92): Dark-theme secondary text, timestamps, and quiet metadata.
- **Paper Light** (#faf9f6): Light-theme page canvas and inverse control text.
- **Rail Light** (#f2f0ea): Light-theme source navigation rail.
- **Surface Light** (#ffffff): Light-theme raised surfaces and controls.
- **Ink Light** (#14110f): Light-theme primary text and primary controls.
- **Graphite Light** (#6b655d): Light-theme secondary text and metadata.
- **Rule Dark** (#332e29) and **Rule Soft Dark** (#24211d): Dark-theme dividers and quiet boundaries.
- **Rule Light** (#ddd8ce) and **Rule Soft Light** (#ebe7df): Light-theme dividers and quiet boundaries.

### Named Rules

**The Vinyl Mark Rule.** Vinyl Red is a signal, not a decoration. Use it for playback position, current-track state, errors, or other meaningful status; do not turn the whole interface red.

**The Tinted Neutral Rule.** Never introduce pure black, pure white, or un-tinted gray as a new surface or text color. Use the existing warm neutrals and their semantic roles.

## 3. Typography

**Display Font:** Archivo (with Avenir, Segoe UI, and system sans fallbacks)  
**Body Font:** Archivo (with Avenir, Segoe UI, and system sans fallbacks)  
**Label/Mono Font:** IBM Plex Mono (with SFMono-Regular and monospace fallbacks)

**Character:** Archivo gives the player a confident, slightly condensed editorial voice without becoming theatrical. IBM Plex Mono reserves precision for timestamps, source labels, counts, and system-like metadata.

### Hierarchy
- **Display** (500, `clamp(32px, 5vw, 44px)`, `.98 line-height`, `-.04em`): Library view titles and primary screen headings.
- **Headline** (520, `28px`, `1.1 line-height`, `-.035em`): Modal and Now Playing titles.
- **Title** (550, `22px`, `1.2 line-height`, `-.025em`): Section headings and important secondary titles.
- **Body** (400, `15px`, `1.45 line-height`): Track names, descriptive copy, and controls that need comfortable reading.
- **Label** (500, `11px`, `.085em`, uppercase): Source types, timestamps, counts, and operational metadata.

### Named Rules

**The Two-Register Rule.** Use Archivo for human content and hierarchy. Use IBM Plex Mono for data, labels, time, and system state. Do not mix the registers within a single phrase without a clear semantic reason.

**The Scan-Length Rule.** Keep prose around 65–75 characters per line where possible. Let track and source names truncate gracefully rather than destabilizing the grid.

## 4. Elevation

The system uses quiet layered shadows over tonal layering. Most library content remains flat and scannable; elevation appears when a surface floats above that content, such as the Now Playing panel, context menu, search results, dialogs, and persistent player. Dark mode uses tinted near-black shadows so surfaces separate without glowing.

### Shadow Vocabulary
- **Flat** (`box-shadow: none`): Library canvas, ordinary rows, and resting structural regions.
- **Edge** (`0 1px 2px var(--shadow-color)`): Small controls, artwork, and active icon-rail items.
- **Panel** (`0 12px 30px var(--shadow-color)`): Search results, Now Playing, and floating utility surfaces.
- **Deep** (`0 24px 72px var(--shadow-color-strong)`): Dialogs and high-priority overlays.
- **Panel seam** (`-18px 0 38px var(--shadow-color), var(--elev-2)`): The docked Now Playing panel, clarifying its boundary with the library.

### Named Rules

**The Structural Shadow Rule.** Shadows explain a surface's relationship to the rest of the player. They should not make flat rows look like cards or add decoration to already-clear boundaries.

## 5. Components

### Buttons
- **Shape:** Compact, rectangular, and tactile with a `6px` control radius.
- **Primary:** Ink background with paper text in light mode, and Old Ivory background with Charred Paper text in dark mode; `40px` height and `0 15px` horizontal padding.
- **Hover / Focus:** Increase border or surface contrast; keyboard focus uses the shared two-step focus ring. Active controls may scale subtly, never bounce.
- **Secondary / Ghost / Tertiary:** Surface-filled secondary buttons use a quiet rule; icon buttons are `40px` square with transparent rest state and tonal hover feedback.

### Cards / Containers
- **Corner Style:** `12px` for panels and dialogs, `6px` for controls, `4px` for artwork and compact surfaces.
- **Background:** Use Paper for the canvas, Rail for navigation, Surface for controls and panels, and Surface Raised only when a layer needs a small tonal lift.
- **Shadow Strategy:** Reference the Edge, Panel, and Deep roles in Elevation. Avoid cardifying ordinary library rows.
- **Border:** Use the warm rule tokens for boundaries and separators; do not add colored side stripes.
- **Internal Padding:** Use the spacing scale, generally `12px`, `18px`, `24px`, or `32px` according to component weight.

### Inputs / Fields
- **Style:** `42px` tall, `12px` horizontal padding, `6px` radius, Paper background, and a warm Rule border.
- **Focus:** Use the shared `--focus-ring` with a clear border shift, never a color-only or glow-only indicator.
- **Error / Disabled:** Use semantic Danger with supporting copy; disabled controls retain their shape and hierarchy while reducing opacity and removing interaction affordances.

### Navigation
- **Style:** The source rail is a stable, left-aligned navigation surface with compact source rows, circular artwork, source metadata, and counts.
- **Default / Hover / Active:** Resting rows are quiet and separated by rules; hover shifts to Surface; active rows use a Surface shift and preserve the Vinyl Mark for playback rather than a colored stripe.
- **Collapsed treatment:** The rail becomes a compact icon navigation column. Labels and counts hide, source action menus become hover/focus overlays, and active items gain a subtle inset boundary.
- **Responsive treatment:** On smaller screens the source rail becomes a drawer above the library and stops above the persistent player.

### Player Bar
- **Shape:** A persistent `96px` floating control surface spanning the shell, with identity, transport, volume, and track actions in the first row and the native progress rail integrated along the lower edge.
- **Hierarchy:** Track identity anchors the left, transport owns the center, and volume plus secondary actions sit to the right. The primary Play control is the strongest control by size and contrast.
- **Behavior:** Keep seeking, playback state, and transport controls available while Now Playing or source navigation changes. Respect reduced motion.

### Track Rows
- **Shape:** Dense `64px` rows with `48px` artwork and aligned mono metadata columns.
- **State:** Ordinary rows stay flat; hover uses a surface shift; the current row uses a restrained Vinyl Red tint and a small playback mark.
- **Overflow:** Track titles, artists, and source names truncate with ellipses while timestamps and counts remain aligned.

## 6. Do's and Don'ts

### Do:
- **Do** keep listening, playback, and track discovery as the clearest path through every screen.
- **Do** use Archivo for hierarchy and IBM Plex Mono for time, counts, labels, and system state.
- **Do** use Vinyl Red only as a meaningful playback or status signal.
- **Do** preserve source provenance near tracks and source navigation.
- **Do** use spacing to separate regions: tight `4–12px` groups, then `18–32px` structural gaps.
- **Do** keep ordinary library content flat and use elevation for floating relationships.
- **Do** provide visible keyboard focus, reduced-motion behavior, usable contrast, and non-color status cues.

### Don't:
- **Don't** make the interface feel like generic streaming-service sameness.
- **Don't** introduce dashboard-like clutter, playful consumer-app decoration, or opaque Telegram utility chrome.
- **Don't** use neon music-player clichés or turn the interface into a glowing entertainment dashboard.
- **Don't** make the interface feel like an admin panel, a social feed, or a marketing landing page.
- **Don't** add visual novelty that competes with the track, artwork, or listening flow.
- **Don't** use pure black, pure white, gradient text, colored side stripes, decorative glassmorphism, or nested cards.
- **Don't** hide essential transport controls on touch screens or rely on color alone to communicate state.
