// Chart colours, read from the CSS tokens in styles.css (QFinVerse palette) when the
// page loads. index.html sets light or dark before any module runs, and a theme
// switch reloads the page, so values read once are always the current theme's.

const TOKENS = {
  primary: "--blue", second: "--teal", amber: "--amber", purple: "--purple",
  up: "--green", down: "--red", muted: "--text-muted", text: "--text",
  border: "--border", surface: "--surface", fill: "--gray-1", grid: "--gray-3",
};

const FALLBACK = {
  primary: "#00AD96", second: "#FD5D0D", amber: "#E6A800", purple: "#8354D4",
  up: "#00A38B", down: "#EE5A14", muted: "#5A6477", text: "#1A1D2E",
  border: "#DDE0E9", surface: "#FFFFFF", fill: "#F1F3F8", grid: "#C9CFDB",
};

function read() {
  const style = typeof document !== "undefined" ? getComputedStyle(document.documentElement) : null;
  const out = {};
  for (const [name, token] of Object.entries(TOKENS)) {
    const v = style ? style.getPropertyValue(token).trim() : "";
    out[name] = /^#[0-9a-fA-F]{6}$/.test(v) ? v : FALLBACK[name];
  }
  return out;
}

export const P = read();
