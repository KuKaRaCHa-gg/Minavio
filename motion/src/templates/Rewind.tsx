import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape} from '../lib/schema';
import {PixelArrow, Stage, clamp, fontFor, hash, pixelUnit, snapTo} from '../lib/pixel';

export const rewindSchema = z.object({
  ...baseShape,
  text: z.string().default('REWIND ◀◀'),
  durationInFrames: z.number().int().positive().default(24),
});
export type RewindProps = z.infer<typeof rewindSchema>;

const RED = '#FF2D55';
const CYAN = '#00E5FF';

/** Texte avec ◀ / ▶ remplacés par des flèches pixel (la fonte n'a pas ces glyphes). */
const GlitchText: React.FC<{text: string; size: number; color: string}> = ({text, size, color}) => (
  <span style={{display: 'inline-flex', alignItems: 'center', whiteSpace: 'pre', color}}>
    {text.split('').map((c, i) =>
      c === '◀' || c === '▶' ? (
        <PixelArrow key={i} size={size} color={color} dir={c === '◀' ? 'left' : 'right'} />
      ) : (
        <span key={i}>{c}</span>
      ),
    )}
  </span>
);

export const Rewind: React.FC<RewindProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const total = p.durationInFrames / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const portrait = p.height > p.width;
  const text = p.text.toUpperCase();

  // Glitch en paliers : l'état change 15 fois par seconde.
  const g = Math.floor(t * 15);
  const intensity = Math.sin(clamp(t / total, 0, 1) * Math.PI); // pic au milieu
  const fadeIn = clamp(Math.floor((t / 0.1) * 3) / 3, 0, 1);
  const fadeOut = clamp(Math.floor(((total - t) / 0.12) * 3) / 3, 0, 1);
  const env = Math.min(fadeIn, fadeOut);
  const bgAlpha = 0.5 * env;

  // Bandes de décalage RGB : blocs horizontaux rouge / cyan déplacés.
  const nBands = Math.round(4 + intensity * 8);
  const bands = Array.from({length: nBands}, (_, i) => ({
    y: snapTo(hash(g * 31 + i) * p.height, unit),
    h: snapTo(unit * (2 + hash(g * 7 + i) * 14), unit),
    shift: snapTo((hash(g * 13 + i) - 0.5) * unit * 28 * (0.4 + intensity), unit),
    c: i % 2 === 0 ? RED : CYAN,
    w: snapTo(p.width * (0.25 + hash(g * 5 + i) * 0.6), unit),
    x: snapTo(hash(g * 3 + i) * p.width * 0.5, unit),
  }));

  // Barre de tracking blanche qui remonte en paliers.
  const trackY = snapTo(p.height * (1 - ((t * 1.4) % 1)), unit * 4);
  const fontSize = snapTo(
    clamp((p.width * (portrait ? 0.76 : 0.5)) / Math.max(8, text.length), unit * 4, unit * 12),
    unit,
  );
  const blinkOn = Math.floor(t * 8) % 2 === 0;
  const jitter = hash(g + 500) > 0.6 ? snapTo((hash(g + 7) - 0.5) * unit * 10, unit) : 0;
  const split = snapTo(unit * (1 + Math.round(hash(g + 3) * 2) + Math.round(intensity * 2)), unit);

  return (
    <Stage unit={unit} scanlines>
      <div style={{position: 'absolute', inset: 0, background: `rgba(10,6,24,${bgAlpha})`}} />
      {bands.map((b, i) => (
        <div
          key={i}
          style={{
            position: 'absolute',
            left: b.x + b.shift,
            top: b.y,
            width: b.w,
            height: b.h,
            background: b.c,
            opacity: 0.38 * env,
          }}
        />
      ))}
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: trackY,
          width: p.width,
          height: unit * 4,
          background: 'rgba(255,255,255,0.35)',
          opacity: env,
        }}
      />
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          width: p.width,
          height: p.height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: fontFor(p.style),
          fontSize,
          opacity: blinkOn || hash(g) > 0.75 ? env : 0,
          transform: `translateX(${jitter}px)`,
        }}
      >
        <div style={{position: 'relative'}}>
          <div style={{position: 'absolute', left: -split, top: 0}}>
            <GlitchText text={text} size={fontSize} color={RED} />
          </div>
          <div style={{position: 'absolute', left: split, top: 0}}>
            <GlitchText text={text} size={fontSize} color={CYAN} />
          </div>
          <div style={{position: 'relative', textShadow: `${unit}px ${unit}px 0 #000`}}>
            <GlitchText text={text} size={fontSize} color="#FFFFFF" />
          </div>
        </div>
      </div>
    </Stage>
  );
};
