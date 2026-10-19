import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape, positionSchema} from '../lib/schema';
import {
  Stage,
  anchorTop,
  beatScale,
  blockShadow,
  clamp,
  fontFor,
  hash,
  pixelUnit,
  safeBox,
  snapTo,
} from '../lib/pixel';

export const titleSchema = z.object({
  ...baseShape,
  text: z.string().default('PRESS START'),
  subtitle: z.string().optional(),
  position: positionSchema.default('center'),
  charsPerSecond: z.number().positive().default(18),
});
export type TitleProps = z.infer<typeof titleSchema>;

const GLITCH_S = 0.35;
const OUT_BLINK_S = 0.4;

export const Title: React.FC<TitleProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const total = p.durationInFrames / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const pixel = p.style === 'pixel';

  const text = p.text.toUpperCase();
  const shown = Math.min(text.length, Math.floor(t * p.charsPerSecond) + 1);
  const typed = text.slice(0, shown);
  const typing = shown < text.length;
  const cursorOn = Math.floor(t * 4) % 2 === 0;

  const fit = box.width / (Math.max(...text.split(' ').map((w) => w.length), 1) + 1);
  const fontSize = snapTo(clamp(fit, unit * 6, unit * 14), unit);
  const subSize = snapTo(Math.max(unit * 3, fontSize * 0.4), unit);
  const lineH = fontSize + unit * 4;
  const blockH = lineH * 2 + subSize * 3;
  const top = anchorTop(box, p.position, blockH);

  // Glitch d'entrée : décalages entiers (multiples du pixel logique) + séparation RGB.
  const glitching = t < GLITCH_S;
  const g = Math.floor(t * 24);
  const jx = glitching ? Math.round((hash(g) - 0.5) * 6) * unit : 0;
  const jy = glitching ? Math.round((hash(g + 99) - 0.5) * 2) * unit : 0;
  const split = glitching ? unit * 2 : 0;

  // Clignotement avant disparition.
  const remaining = total - t;
  const hidden = remaining < OUT_BLINK_S && Math.floor(remaining * 10) % 2 === 0;
  const scale = beatScale(t, p.beats);
  const color = '#FFFFFF';

  const shadow = glitching
    ? `${-split}px 0 0 ${p.theme.primary}, ${split}px 0 0 ${p.theme.accent}`
    : blockShadow(unit, p.theme.primary, '#000000');

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div
        style={{
          position: 'absolute',
          left: box.left + jx,
          top: top + jy,
          width: box.width,
          textAlign: 'center',
          opacity: hidden ? 0 : 1,
          fontFamily: fontFor(p.style),
          transform: `scale(${scale})`,
          transformOrigin: '50% 50%',
        }}
      >
        <div
          style={{
            fontSize,
            lineHeight: `${lineH}px`,
            color,
            textShadow: shadow,
            wordBreak: 'keep-all',
            minHeight: lineH,
          }}
        >
          {typed}
          {pixel && (typing || cursorOn) ? (
            <span style={{color: p.theme.accent}}>_</span>
          ) : null}
        </div>
        {p.subtitle ? (
          <div
            style={{
              marginTop: unit * 4,
              fontSize: subSize,
              color: p.theme.accent,
              textShadow: blockShadow(unit, '#000000', null),
              visibility: !typing && Math.floor(t * 3) % 2 === 0 ? 'visible' : 'hidden',
            }}
          >
            {p.subtitle.toUpperCase()}
          </div>
        ) : null}
      </div>
    </Stage>
  );
};
