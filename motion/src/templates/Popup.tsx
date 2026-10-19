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
  pixelUnit,
  popScale,
  safeBox,
  snapTo,
} from '../lib/pixel';

export const popupSchema = z.object({
  ...baseShape,
  text: z.string().default('+1 KILL'),
  position: positionSchema.default('top'),
});
export type PopupProps = z.infer<typeof popupSchema>;

const OUT_S = 0.25;

export const Popup: React.FC<PopupProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const total = p.durationInFrames / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const pixel = p.style === 'pixel';
  const text = p.text.toUpperCase();

  const fit = (box.width * 0.9) / (text.length + 2);
  const fontSize = snapTo(clamp(fit, unit * 4, unit * 10), unit);
  const padX = unit * 4;
  const padY = unit * 3;
  const boxW = snapTo(text.length * fontSize * (pixel ? 1 : 0.62) + padX * 2, unit);
  const boxH = fontSize + padY * 2;
  const top = anchorTop(box, p.position, boxH + unit * 2);
  const left = snapTo(box.left + (box.width - boxW) / 2, unit);

  // Entrée en pop, sortie en 3 paliers de réduction, pop supplémentaire sur chaque temps fort.
  const remaining = total - t;
  const outStep = remaining < OUT_S ? Math.floor((1 - remaining / OUT_S) * 3) : 0;
  const outScale = [1, 0.75, 0.5][outStep];
  const scale = popScale(t, 0) * beatScale(t, p.beats) * outScale;

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div
        style={{
          position: 'absolute',
          left,
          top,
          width: boxW,
          height: boxH,
          boxSizing: 'border-box',
          background: p.theme.primary,
          color: '#FFFFFF',
          fontFamily: fontFor(p.style),
          fontSize,
          lineHeight: `${boxH}px`,
          textAlign: 'center',
          whiteSpace: 'nowrap',
          border: `${unit}px solid #000000`,
          // Coins « crantés » + ombre en bloc.
          boxShadow: `${unit}px 0 0 0 #000, -${unit}px 0 0 0 #000, 0 ${unit}px 0 0 #000, 0 -${unit}px 0 0 #000, inset 0 -${unit * 2}px 0 0 rgba(0,0,0,0.25), inset 0 ${unit}px 0 0 ${p.theme.accent}, ${unit * 3}px ${unit * 3}px 0 0 rgba(0,0,0,0.55)`,
          textShadow: blockShadow(unit, '#000000', null).split(', ')[0],
          transform: `scale(${scale})`,
          transformOrigin: '50% 50%',
        }}
      >
        {text}
      </div>
    </Stage>
  );
};
