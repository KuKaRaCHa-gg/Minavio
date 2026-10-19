import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape, positionSchema} from '../lib/schema';
import {
  Stage,
  anchorTop,
  blockShadow,
  clamp,
  fontFor,
  pixelUnit,
  popScale,
  safeBox,
  snapTo,
} from '../lib/pixel';

export const captionsSchema = z.object({
  ...baseShape,
  // Temps relatifs au début de l'overlay (secondes).
  words: z
    .array(z.object({text: z.string(), start: z.number(), end: z.number()}))
    .default([]),
  position: positionSchema.default('bottom'),
});
export type CaptionsProps = z.infer<typeof captionsSchema>;

export const Captions: React.FC<CaptionsProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const words = p.words;
  const pixel = p.style === 'pixel';

  // Mot courant : celui en cours, sinon le dernier déjà prononcé (avant le 1er : aucun).
  let current = -1;
  words.forEach((w, i) => {
    if (w.start <= t) current = i;
  });

  const chars = Math.max(
    1,
    words.reduce((n, w) => n + w.text.length, 0) + Math.max(0, words.length - 1),
  );
  const fitFont = pixel ? box.width / (chars + 1) : box.width / (chars * 0.62);
  const fontSize = snapTo(clamp(fitFont, unit * 4, unit * 11), unit);
  const lineHeight = fontSize + unit * 3;
  const approxLines = Math.max(1, Math.ceil((chars * fontSize * (pixel ? 1 : 0.62)) / box.width));
  const blockH = lineHeight * approxLines + unit * 4;
  const top = anchorTop(box, p.position, blockH);

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div
        style={{
          position: 'absolute',
          left: box.left,
          top,
          width: box.width,
          display: 'flex',
          flexWrap: 'wrap',
          justifyContent: 'center',
          alignItems: 'center',
          columnGap: snapTo(fontSize * (pixel ? 1.0 : 0.35), unit),
          rowGap: unit * 2,
          fontFamily: fontFor(p.style),
          fontSize,
          lineHeight: `${lineHeight}px`,
          textTransform: 'uppercase',
          textAlign: 'center',
        }}
      >
        {words.map((w, i) => {
          const active = i === current;
          const scale = active ? popScale(t, w.start) : 1;
          return (
            <span
              key={`${i}-${w.text}`}
              style={{
                display: 'inline-block',
                visibility: w.start <= t ? 'visible' : 'hidden',
                color: active ? p.theme.accent : '#FFFFFF',
                textShadow: blockShadow(unit, p.theme.primary, '#000000'),
                transform: `scale(${scale})`,
                transformOrigin: '50% 80%',
                WebkitTextStroke: pixel ? undefined : `${unit}px #000`,
                paintOrder: 'stroke fill',
              }}
            >
              {w.text}
            </span>
          );
        })}
      </div>
    </Stage>
  );
};
