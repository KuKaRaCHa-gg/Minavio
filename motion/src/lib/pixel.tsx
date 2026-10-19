import React from 'react';
import {AbsoluteFill, continueRender, delayRender, staticFile} from 'remotion';

// Fonte embarquée (motion/public/fonts, licence OFL) : aucun accès réseau au rendu.
const PIXEL_FONT = 'Press Start 2P';
const pixelFamily = `"${PIXEL_FONT}", monospace`;

if (typeof FontFace !== 'undefined') {
  const handle = delayRender('Chargement de la fonte pixel');
  const face = new FontFace(
    PIXEL_FONT,
    `url('${staticFile('fonts/PressStart2P-Regular.ttf')}') format('truetype')`,
  );
  face
    .load()
    .then(() => (document.fonts as unknown as {add: (f: FontFace) => void}).add(face))
    .catch((err) => console.warn('Fonte pixel indisponible, repli monospace', err))
    .finally(() => continueRender(handle));
}

export const CLEAN_FAMILY = '"Arial Black", "Helvetica Neue", Arial, sans-serif';

export const fontFor = (style: 'pixel' | 'clean'): string =>
  style === 'pixel' ? pixelFamily : CLEAN_FAMILY;

/** Taille d'un « pixel logique » : tout est arrondi à ce pas. */
export const pixelUnit = (width: number, height: number): number =>
  Math.max(2, Math.round(Math.min(width, height) / 180));

export const snapTo = (v: number, unit: number): number => Math.round(v / unit) * unit;

export const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/** Progression 0..1 découpée en `steps` paliers. */
export const stepped = (p: number, steps: number): number =>
  Math.floor(clamp(p, 0, 1) * steps) / steps;

/** Hash déterministe (glitch reproductible entre deux rendus). */
export const hash = (n: number): number => {
  const x = Math.sin(n * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
};

const POP_TABLE = [1.45, 1.3, 1.15, 1.05, 1.0];
const POP_STEP_S = 1 / 16;

/** Pop 8-bit : échelle en paliers après l'instant `at` (s). 1 hors fenêtre. */
export const popScale = (t: number, at: number): number => {
  const since = t - at;
  if (since < 0) return 1;
  const idx = Math.floor(since / POP_STEP_S);
  return idx < POP_TABLE.length ? POP_TABLE[idx] : 1;
};

/** Pop du dernier temps fort passé (beats en secondes relatives à l'overlay). */
export const beatScale = (t: number, beats: number[]): number => {
  let last = -Infinity;
  for (const b of beats) if (b <= t && b > last) last = b;
  return last === -Infinity ? 1 : 1 + (popScale(t, last) - 1) * 0.6;
};

export interface SafeBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * Zone sûre. En 9:16 : on évite les ~15 % du bas (description/boutons TikTok) et la colonne
 * droite d'UI (like/commentaire), ~14 % de la largeur.
 */
export const safeBox = (width: number, height: number): SafeBox => {
  if (height > width) {
    const left = Math.round(width * 0.06);
    const right = Math.round(width * 0.82);
    const top = Math.round(height * 0.08);
    const bottom = Math.round(height * 0.85);
    return {left, top, width: right - left, height: bottom - top};
  }
  const left = Math.round(width * 0.05);
  const top = Math.round(height * 0.06);
  return {left, top, width: width - 2 * left, height: Math.round(height * 0.88)};
};

/** Ombre / contour en blocs, sans flou. */
export const blockShadow = (unit: number, color: string, outline: string | null): string => {
  const parts: string[] = [];
  if (outline) {
    for (const [dx, dy] of [
      [-1, 0],
      [1, 0],
      [0, -1],
      [0, 1],
      [-1, -1],
      [1, -1],
      [-1, 1],
      [1, 1],
    ])
      parts.push(`${dx * unit}px ${dy * unit}px 0 ${outline}`);
  }
  parts.push(`${unit}px ${unit}px 0 ${color}`, `${2 * unit}px ${2 * unit}px 0 ${color}`);
  return parts.join(', ');
};

/** Fond transparent + rendu net + scanlines CRT optionnelles. */
export const Stage: React.FC<{
  unit: number;
  scanlines?: boolean;
  children: React.ReactNode;
}> = ({unit, scanlines, children}) => (
  <AbsoluteFill style={{imageRendering: 'pixelated'}}>
    {children}
    {scanlines ? (
      <AbsoluteFill
        style={{
          pointerEvents: 'none',
          backgroundImage: `repeating-linear-gradient(to bottom, rgba(0,0,0,0.16) 0px, rgba(0,0,0,0.16) ${unit}px, rgba(0,0,0,0) ${unit}px, rgba(0,0,0,0) ${unit * 2}px)`,
        }}
      />
    ) : null}
  </AbsoluteFill>
);

/** Position verticale d'un bloc dans la zone sûre. */
export const anchorTop = (
  box: SafeBox,
  position: 'top' | 'center' | 'bottom',
  blockHeight: number,
): number => {
  if (position === 'top') return box.top;
  if (position === 'center') return box.top + Math.round((box.height - blockHeight) / 2);
  return box.top + box.height - blockHeight;
};
