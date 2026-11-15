// Readable tones of a scheme's accent. The accent itself is for borders, focus rings, icons and marks (3:1).
// Text needs 4.5:1 (WCAG 2.2 AA, 1.4.3), so text on an accent fill, and accent-colored text, use these tones.
// Each tone is the accent moved toward black or white by the smallest step that passes: the hue stays.

const channel = (hex: string, at: number) => {
  const value = parseInt(hex.slice(at, at + 2), 16) / 255
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
}
export const luminance = (hex: string) => 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5)
export function contrast(one: string, two: string) {
  const [a, b] = [luminance(one), luminance(two)].sort((x, y) => y - x)
  return (a + 0.05) / (b + 0.05)
}
/** `share` of one color over another, like color-mix(in srgb, one share%, two). Colors are #rrggbb. */
export const mix = (one: string, two: string, share: number) =>
  '#' + [1, 3, 5].map(at => Math.round(parseInt(one.slice(at, at + 2), 16) * share + parseInt(two.slice(at, at + 2), 16) * (1 - share)).toString(16).padStart(2, '0')).join('')

export const TEXT_CONTRAST = 4.5
export const HOVER_SHARE = 0.86  // controls.css: --accent-hover is 86% fill, 14% black
export const TINT_SHARE = 0.16   // the strongest accent tint that carries accent text (badges, pills, counts)

function nearest(accent: string, passes: (tone: string) => boolean) {
  for (let step = 0; step <= 100; step++) {
    for (const end of ['#000000', '#ffffff']) {
      const tone = mix(end, accent, step / 100)
      if (passes(tone)) return tone
    }
  }
  return accent
}

/** The fill of a filled control. Text in `accentText` reads on it, and on its hover shade. */
export const accentFill = (accent: string, accentText: string) =>
  nearest(accent, tone => contrast(tone, accentText) >= TEXT_CONTRAST && contrast(mix(tone, '#000000', HOVER_SHARE), accentText) >= TEXT_CONTRAST)

/** Accent-colored text. It reads on each surface, and on an accent tint of each surface. */
export const accentInk = (accent: string, surfaces: string[]) =>
  nearest(accent, tone => surfaces.every(surface => contrast(tone, surface) >= TEXT_CONTRAST && contrast(tone, mix(accent, surface, TINT_SHARE)) >= TEXT_CONTRAST))
