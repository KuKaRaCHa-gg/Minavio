import React from 'react';
import {AbsoluteFill, Img, staticFile} from 'remotion';
import {z} from 'zod';
import {themeSchema} from '../lib/schema';
import {PixelSkull, fontFor, snapTo} from '../lib/pixel';

/**
 * Miniature YouTube / cover Short : composition Remotion `Still` (une seule image).
 *
 * Images : chaque entrée de `images` est soit une data URI / URL http(s) (utilisable telle quelle),
 * soit un chemin relatif résolu par `staticFile()` (donc dans `motion/public/`). Un chemin disque
 * ou `file://` n'est PAS lisible par Chromium depuis la page de rendu : le bridge Python
 * (`minavio.motion.thumbnail`) les convertit en data URI avant d'écrire les props.
 */
export const thumbnailSchema = z.object({
  layout: z.enum(['hero', 'split', 'collage']).default('hero'),
  images: z.array(z.string().min(1)).min(1).max(3),
  text: z.string().default(''),
  badge: z.string().optional(),
  kills: z.number().int().min(0).max(99).optional(),
  highlight: z.object({x: z.number().min(0).max(1), y: z.number().min(0).max(1)}).optional(),
  theme: themeSchema.default({}),
  width: z.number().int().positive().default(1280),
  height: z.number().int().positive().default(720),
  fps: z.number().positive().default(30),
  durationInFrames: z.number().int().positive().default(1),
});
export type ThumbnailProps = z.infer<typeof thumbnailSchema>;
type Theme = ThumbnailProps['theme'];
type Box = {left: number; top: number; width: number; height: number};

const INK = '#000000';
const BG = '#10101c';
const FONT = 'pixel' as const;

export const resolveSrc = (s: string): string =>
  /^(data:|https?:|blob:)/.test(s) ? s : staticFile(s.replace(/^\/+/, ''));

/** Contour épais en blocs (grille de décalages) + ombre portée en escalier. */
export const thumbShadow = (o: number, shadow: string): string => {
  const parts: string[] = [];
  const steps = [-o, -o / 2, 0, o / 2, o];
  for (const dx of steps)
    for (const dy of steps)
      if ((dx !== 0 || dy !== 0) && Math.hypot(dx, dy) <= o * 1.45) parts.push(`${dx}px ${dy}px 0 ${INK}`);
  for (let k = 1; k <= 3; k++) parts.push(`${o + (o * k) / 2}px ${o + (o * k) / 2}px 0 ${shadow}`);
  return parts.join(', ');
};

/** Coupe le texte en 1..3 lignes (mots entiers) et garde la découpe au plus gros corps. */
export const fitText = (
  text: string,
  maxW: number,
  maxH: number,
  maxFont: number,
): {lines: string[]; fontSize: number} => {
  const words = text.trim().toUpperCase().split(/\s+/).filter(Boolean);
  if (!words.length) return {lines: [], fontSize: 0};
  const n = words.length;
  const partitions = (k: number): string[][] => {
    const out: string[][] = [];
    const rec = (start: number, left: number, acc: string[]) => {
      if (left === 1) {
        out.push([...acc, words.slice(start).join(' ')]);
        return;
      }
      for (let e = start + 1; e <= n - left + 1; e++) rec(e, left - 1, [...acc, words.slice(start, e).join(' ')]);
    };
    rec(0, k, []);
    return out;
  };
  let best = {lines: [words.join(' ')], fontSize: 0};
  for (let k = 1; k <= Math.min(3, n); k++)
    for (const lines of partitions(k)) {
      const longest = Math.max(...lines.map((l) => l.length));
      const fs = Math.min(maxFont, maxW / longest, maxH / (lines.length * 1.35));
      if (fs > best.fontSize) best = {lines, fontSize: fs};
    }
  return {lines: best.lines, fontSize: Math.floor(best.fontSize)};
};

const GRADE = 'saturate(1.35) contrast(1.12) brightness(1.04)';

/** Image « cover » sur-saturée + vignette, zoom optionnel autour de (ox, oy) normalisés. */
const Photo: React.FC<{src: string; zoom?: number; ox?: number; oy?: number; vignette?: number}> = ({
  src,
  zoom = 1,
  ox = 0.5,
  oy = 0.5,
  vignette = 0.6,
}) => (
  <AbsoluteFill style={{overflow: 'hidden', background: BG}}>
    <Img
      src={resolveSrc(src)}
      style={{
        width: '100%',
        height: '100%',
        objectFit: 'cover',
        objectPosition: `${ox * 100}% ${oy * 100}%`,
        filter: GRADE,
        transform: `scale(${zoom})`,
        transformOrigin: `${ox * 100}% ${oy * 100}%`,
      }}
    />
    <AbsoluteFill
      style={{background: `radial-gradient(ellipse at 50% 50%, rgba(0,0,0,0) 52%, rgba(0,0,0,${vignette}) 100%)`}}
    />
  </AbsoluteFill>
);

const Frame: React.FC<{box: Box; u: number; color: string; children: React.ReactNode}> = ({
  box,
  u,
  color,
  children,
}) => (
  <div
    style={{
      position: 'absolute',
      ...box,
      boxShadow: `0 0 0 ${u}px ${color}, 0 0 0 ${u * 2}px ${INK}, ${u * 3}px ${u * 3}px 0 ${u * 2}px rgba(0,0,0,0.6)`,
      overflow: 'hidden',
      background: BG,
    }}
  >
    {children}
  </div>
);

/** Gros texte 2-4 mots : dernière ligne en accent, contour noir épais, ombre en blocs. */
const BigText: React.FC<{
  text: string;
  box: Box;
  u: number;
  theme: Theme;
  align?: 'left' | 'center';
  valign?: 'top' | 'bottom';
}> = ({text, box, u, theme, align = 'left', valign = 'bottom'}) => {
  const fit = fitText(text, box.width - u * 3, box.height, u * 22);
  if (!fit.lines.length) return null;
  const fs = fit.fontSize;
  const o = Math.max(u * 0.75, Math.round(fs / 14));
  return (
    <div
      style={{
        position: 'absolute',
        ...box,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: valign === 'top' ? 'flex-start' : 'flex-end',
        alignItems: align === 'center' ? 'center' : 'flex-start',
        fontFamily: fontFor(FONT),
        fontSize: fs,
        lineHeight: `${Math.round(fs * 1.3)}px`,
        whiteSpace: 'nowrap',
      }}
    >
      {fit.lines.map((line, i) => (
        <div
          key={i}
          style={{
            color: i === fit.lines.length - 1 ? theme.accent : '#FFFFFF',
            textShadow: thumbShadow(o, theme.primary),
          }}
        >
          {line}
        </div>
      ))}
    </div>
  );
};

const Badge: React.FC<{text: string; x: number; y: number; u: number; theme: Theme}> = ({text, x, y, u, theme}) => {
  const fs = u * 3;
  return (
    <div
      style={{
        position: 'absolute',
        left: x,
        top: y,
        padding: `${u * 1.5}px ${u * 2.5}px`,
        background: theme.primary,
        color: '#FFFFFF',
        fontFamily: fontFor(FONT),
        fontSize: fs,
        lineHeight: `${fs}px`,
        whiteSpace: 'nowrap',
        boxShadow: `0 0 0 ${u * 0.75}px ${INK}, inset 0 0 0 ${u * 0.5}px ${theme.accent}, ${u * 1.5}px ${u * 1.5}px 0 ${u * 0.75}px rgba(0,0,0,0.65)`,
        textShadow: `${u / 2}px ${u / 2}px 0 ${INK}`,
      }}
    >
      {text.toUpperCase()}
    </div>
  );
};

/** Crânes pixel (max 5 ; au-delà, un crâne + « xN »), cartouche noir bordé de la couleur primaire. */
const Kills: React.FC<{kills: number; right: number; y: number; u: number; theme: Theme}> = ({
  kills,
  right,
  y,
  u,
  theme,
}) => {
  if (kills <= 0) return null;
  const compact = kills > 5;
  return (
    <div
      style={{
        position: 'absolute',
        top: y,
        right,
        display: 'flex',
        alignItems: 'center',
        gap: u,
        padding: u * 1.5,
        background: 'rgba(0,0,0,0.85)',
        boxShadow: `0 0 0 ${u * 0.75}px ${theme.primary}, 0 0 0 ${u * 1.5}px ${INK}`,
      }}
    >
      {Array.from({length: compact ? 1 : kills}, (_, i) => (
        <PixelSkull key={i} px={Math.max(2, Math.round(u * 0.9))} color={theme.accent} hole={theme.primary} outline={INK} />
      ))}
      {compact ? (
        <span
          style={{
            fontFamily: fontFor(FONT),
            fontSize: u * 5,
            color: theme.accent,
            textShadow: `${u / 2}px ${u / 2}px 0 ${theme.primary}`,
          }}
        >
          x{kills}
        </span>
      ) : null}
    </div>
  );
};

/** Anneau pixel (cellules carrées, contour noir) centré sur (cx, cy). */
const PixelRing: React.FC<{cx: number; cy: number; r: number; cell: number; color: string}> = ({cx, cy, r, cell, color}) => {
  const n = Math.ceil(r / cell) + 2;
  const cells: React.ReactNode[] = [];
  const inner = r - cell * 1.6;
  for (let j = -n; j <= n; j++)
    for (let i = -n; i <= n; i++) {
      const d = Math.hypot(i, j) * cell;
      if (d <= r && d >= inner) cells.push(<rect key={`${i},${j}`} x={i * cell} y={j * cell} width={cell} height={cell} />);
    }
  const size = (n * 2 + 1) * cell;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`${-n * cell - cell / 2} ${-n * cell - cell / 2} ${size} ${size}`}
      shapeRendering="crispEdges"
      style={{position: 'absolute', left: cx - size / 2, top: cy - size / 2, overflow: 'visible'}}
    >
      <g fill={INK} stroke={INK} strokeWidth={cell * 0.9}>
        {cells}
      </g>
      <g fill={color}>{cells}</g>
    </svg>
  );
};

const ARROW = ['0001000', '0001100', '0001110', '1111111', '0001110', '0001100', '0001000'];

/** Flèche pixel pointant vers (tx, ty), posée du côté le plus dégagé. */
const PixelPointer: React.FC<{tx: number; ty: number; w: number; r: number; cell: number; color: string}> = ({
  tx,
  ty,
  w,
  r,
  cell,
  color,
}) => {
  const fromLeft = tx > w * 0.35;
  const size = ARROW.length * cell;
  const left = fromLeft ? tx - r - size - cell : tx + r + cell;
  const rects: React.ReactNode[] = [];
  ARROW.forEach((row, y) =>
    row.split('').forEach((c, x) => {
      if (c === '1') rects.push(<rect key={`${x},${y}`} x={fromLeft ? x : ARROW.length - 1 - x} y={y} width={1} height={1} />);
    }),
  );
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${ARROW.length} ${ARROW.length}`}
      shapeRendering="crispEdges"
      style={{position: 'absolute', left, top: ty - size / 2, overflow: 'visible'}}
    >
      <g fill={INK} stroke={INK} strokeWidth={0.9}>
        {rects}
      </g>
      <g fill={color}>{rects}</g>
    </svg>
  );
};

/** Moitié « avant » de la diagonale crénelée, dans un repère paysage A (axe long) x B. */
const stairPoints = (A: number, B: number, step: number, skew: number, offset: number): [number, number][] => {
  const pts: [number, number][] = [[0, 0]];
  const n = Math.ceil(B / step);
  for (let i = 0; i < n; i++) {
    const b0 = i * step;
    const b1 = Math.min(B, (i + 1) * step);
    const a = snapTo(A / 2 - ((b0 + b1) / 2 / B - 0.5) * skew, step) + offset;
    pts.push([a, b0], [a, b1]);
  }
  pts.push([0, B]);
  return pts;
};

const toClip = (pts: [number, number][], portrait: boolean): string =>
  `polygon(${pts.map(([a, b]) => (portrait ? `${b}px ${a}px` : `${a}px ${b}px`)).join(', ')})`;

export const Thumbnail: React.FC<ThumbnailProps> = (p) => {
  const {width: W, height: H, theme} = p;
  const portrait = H > W;
  const u = Math.max(3, Math.round(Math.min(W, H) / 90));
  const imgs = [0, 1, 2].map((i) => p.images[i % p.images.length]);
  const m = Math.round(Math.min(W, H) * 0.035);
  // Zones sûres : 16:9 -> coin bas-droit masqué par la durée ; 9:16 -> bas et colonne droite
  // masqués par l'UI Shorts/TikTok. Le texte reste donc en haut (9:16) / bas-gauche (16:9).
  const safeR = portrait ? Math.round(W * 0.14) : 0;
  const topY = portrait ? Math.round(H * 0.04) : m;
  const textW = W - m * 2 - safeR;
  const portraitText: Box = {left: m, top: Math.round(H * 0.11), width: textW, height: Math.round(H * 0.24)};

  let body: React.ReactNode;

  if (p.layout === 'hero') {
    const hx = p.highlight?.x ?? 0.5;
    const hy = p.highlight?.y ?? 0.42;
    const textBox: Box = portrait
      ? portraitText
      : {left: m, top: Math.round(H * 0.5), width: Math.round(W * 0.7), height: Math.round(H * 0.46)};
    const r = Math.round(Math.min(W, H) * 0.13);
    const cell = Math.max(2, Math.round(u * 0.9));
    body = (
      <>
        <Photo src={imgs[0]} zoom={1.12} ox={hx} oy={hy} vignette={0.7} />
        <AbsoluteFill
          style={{
            background: portrait
              ? 'linear-gradient(to bottom, rgba(0,0,0,0.65) 0%, rgba(0,0,0,0) 45%)'
              : 'linear-gradient(to top, rgba(0,0,0,0.7) 0%, rgba(0,0,0,0) 55%)',
          }}
        />
        {p.highlight ? (
          <>
            <PixelRing cx={hx * W} cy={hy * H} r={r} cell={cell} color={theme.accent} />
            <PixelPointer tx={hx * W} ty={hy * H} w={W} r={r} cell={cell * 2} color={theme.primary} />
          </>
        ) : null}
        <BigText text={p.text} box={textBox} u={u} theme={theme} valign={portrait ? 'top' : 'bottom'} />
      </>
    );
  } else if (p.layout === 'split') {
    const step = u * 3;
    const [A, B] = portrait ? [H, W] : [W, H];
    const skew = A * 0.14;
    const band = u * 2.5;
    const half = Math.round(A * 0.6);
    const textBox: Box = portrait
      ? {...portraitText, left: m, width: textW}
      : {left: m, top: Math.round(H * 0.72), width: W - m * 2, height: Math.round(H * 0.25)};
    const vs = Math.round(Math.min(W, H) * (portrait ? 0.2 : 0.3));
    const pos = (second: boolean): React.CSSProperties =>
      portrait
        ? {position: 'absolute', left: 0, top: second ? H - half : 0, width: W, height: half}
        : {position: 'absolute', top: 0, left: second ? W - half : 0, width: half, height: H};
    body = (
      <>
        <div style={pos(true)}>
          <Photo src={imgs[1]} ox={0.5} oy={0.4} />
        </div>
        <AbsoluteFill style={{clipPath: toClip(stairPoints(A, B, step, skew, band + u), portrait), background: INK}} />
        <AbsoluteFill style={{clipPath: toClip(stairPoints(A, B, step, skew, band), portrait), background: theme.accent}} />
        <AbsoluteFill style={{clipPath: toClip(stairPoints(A, B, step, skew, 0), portrait)}}>
          <div style={pos(false)}>
            <Photo src={imgs[0]} ox={0.5} oy={0.4} />
          </div>
        </AbsoluteFill>
        <AbsoluteFill
          style={{
            background: portrait
              ? 'linear-gradient(to bottom, rgba(0,0,0,0.6) 0%, rgba(0,0,0,0) 32%)'
              : 'linear-gradient(to top, rgba(0,0,0,0.7) 0%, rgba(0,0,0,0) 40%)',
          }}
        />
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontFamily: fontFor(FONT),
            fontSize: vs,
            color: theme.accent,
            textShadow: thumbShadow(Math.round(vs / 12), theme.primary),
            paddingBottom: portrait ? 0 : Math.round(H * 0.05),
            paddingLeft: Math.round(vs / 6),
          }}
        >
          VS
        </div>
        <BigText text={p.text} box={textBox} u={u} theme={theme} align="center" valign={portrait ? 'top' : 'bottom'} />
      </>
    );
  } else {
    const g = u * 3;
    const b = u * 2;
    let big: Box;
    let smalls: Box[];
    let textBox: Box;
    if (portrait) {
      const x0 = m + b;
      const usableW = W - 2 * x0;
      const bigTop = Math.round(H * 0.17);
      big = {left: x0, top: bigTop, width: usableW, height: Math.round(H * 0.47)};
      const smallW = Math.floor((usableW - g - b * 2) / 2);
      const smallTop = big.top + big.height + g + b * 2;
      const smallH = Math.round(H * 0.87) - smallTop;
      smalls = [
        {left: x0, top: smallTop, width: smallW, height: smallH},
        {left: x0 + smallW + g + b * 2, top: smallTop, width: smallW, height: smallH},
      ];
      textBox = {left: x0, top: Math.round(H * 0.1), width: usableW - safeR, height: Math.round(H * 0.15)};
    } else {
      const usableH = H - 2 * (m + b);
      const bigW = Math.round(W * 0.62);
      big = {left: m + b, top: m + b, width: bigW, height: usableH};
      const sx = big.left + bigW + g + b * 2;
      const sw = W - m - b - sx;
      const sh = Math.floor((usableH - g - b * 2) / 2);
      smalls = [
        {left: sx, top: m + b, width: sw, height: sh},
        {left: sx, top: m + b + sh + g + b * 2, width: sw, height: sh},
      ];
      textBox = {left: big.left + u * 2, top: Math.round(H * 0.45), width: bigW - u * 4, height: Math.round(H * 0.46)};
    }
    body = (
      <>
        <AbsoluteFill
          style={{
            background: `repeating-linear-gradient(135deg, ${BG} 0px, ${BG} ${u * 4}px, ${theme.primary}40 ${u * 4}px, ${theme.primary}40 ${u * 8}px)`,
          }}
        />
        <Frame box={big} u={u} color={theme.accent}>
          <Photo src={imgs[0]} zoom={1.08} ox={p.highlight?.x ?? 0.5} oy={p.highlight?.y ?? 0.4} vignette={0.7} />
          <AbsoluteFill
            style={{
              background: portrait
                ? 'linear-gradient(to bottom, rgba(0,0,0,0.6) 0%, rgba(0,0,0,0) 40%)'
                : 'linear-gradient(to top, rgba(0,0,0,0.7) 0%, rgba(0,0,0,0) 55%)',
            }}
          />
        </Frame>
        {smalls.map((s, i) => (
          <Frame key={i} box={s} u={u} color={i === 0 ? theme.primary : '#FFFFFF'}>
            <Photo src={imgs[i + 1]} ox={0.5} oy={0.4} />
          </Frame>
        ))}
        <BigText text={p.text} box={textBox} u={u} theme={theme} valign={portrait ? 'top' : 'bottom'} />
      </>
    );
  }

  return (
    <AbsoluteFill style={{background: BG}}>
      {body}
      {p.badge ? <Badge text={p.badge} x={m} y={topY} u={u} theme={theme} /> : null}
      {p.kills ? <Kills kills={p.kills} right={m + safeR} y={topY} u={u} theme={theme} /> : null}
    </AbsoluteFill>
  );
};
