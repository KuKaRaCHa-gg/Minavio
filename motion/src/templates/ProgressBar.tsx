import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape, positionSchema} from '../lib/schema';
import {Stage, anchorTop, beatScale, blockShadow, fontFor, pixelUnit, safeBox, snapTo} from '../lib/pixel';

export const progressBarSchema = z.object({
  ...baseShape,
  segments: z.number().int().min(2).max(40).default(12),
  label: z.string().default(''),
  // Fixe (0..1) ; sinon la barre se remplit linéairement sur la durée de l'overlay.
  progress: z.number().min(0).max(1).optional(),
  position: positionSchema.default('top'),
});
export type ProgressBarProps = z.infer<typeof progressBarSchema>;

export const ProgressBar: React.FC<ProgressBarProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const pixel = p.style === 'pixel';

  const ratio = p.progress ?? Math.min(1, frame / Math.max(1, p.durationInFrames - 1));
  const filled = Math.floor(ratio * p.segments + 1e-6);
  const low = filled / p.segments < 0.25;
  const blinkOff = low && Math.floor(t * 4) % 2 === 1;

  const labelSize = snapTo(unit * 4, unit);
  const barH = unit * 8;
  const gap = unit;
  const labelH = p.label ? labelSize + unit * 3 : 0;
  const blockH = barH + labelH + unit * 4;
  const segW = Math.floor((box.width - unit * 4 - gap * (p.segments - 1)) / p.segments / unit) * unit;
  const barW = segW * p.segments + gap * (p.segments - 1) + unit * 4;
  const left = snapTo(box.left + (box.width - barW) / 2, unit);
  const top = anchorTop(box, p.position, blockH);
  const fillColor = low ? p.theme.primary : p.theme.accent;
  const flash = beatScale(t, p.beats) > 1.05;

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div style={{position: 'absolute', left, top, width: barW, fontFamily: fontFor(p.style)}}>
        {p.label ? (
          <div
            style={{
              fontSize: labelSize,
              lineHeight: `${labelSize}px`,
              height: labelH,
              color: '#FFFFFF',
              textShadow: blockShadow(unit, '#000000', null).split(', ')[0],
            }}
          >
            {p.label.toUpperCase()}
          </div>
        ) : null}
        <div
          style={{
            display: 'flex',
            gap,
            padding: unit * 2,
            height: barH + unit * 4,
            boxSizing: 'border-box',
            background: '#000000',
            border: `${unit}px solid ${flash ? '#FFFFFF' : p.theme.primary}`,
            boxShadow: `${unit * 2}px ${unit * 2}px 0 0 rgba(0,0,0,0.55)`,
            alignContent: 'center',
          }}
        >
          {Array.from({length: p.segments}, (_, i) => (
            <div
              key={i}
              style={{
                width: segW,
                height: barH - unit * 2,
                background: i < filled ? (blinkOff ? '#FFFFFF' : fillColor) : 'rgba(255,255,255,0.14)',
                boxShadow: pixel && i < filled ? `inset 0 ${unit}px 0 0 rgba(255,255,255,0.45)` : undefined,
              }}
            />
          ))}
        </div>
      </div>
    </Stage>
  );
};
