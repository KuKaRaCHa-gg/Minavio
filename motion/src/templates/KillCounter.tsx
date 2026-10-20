import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape} from '../lib/schema';
import {
  PixelSkull,
  Stage,
  beatScale,
  blockShadow,
  clamp,
  fontFor,
  hash,
  pixelUnit,
  popScale,
  safeBox,
  snapTo,
} from '../lib/pixel';

export const killCounterSchema = z.object({
  ...baseShape,
  kills: z.number().int().min(1).max(5).default(1),
  label: z.string().optional(),
  durationInFrames: z.number().int().positive().default(60),
});
export type KillCounterProps = z.infer<typeof killCounterSchema>;

const DEFAULT_LABELS = ['KILL', 'DOUBLE KILL', 'TRIPLE KILL', 'SQUAD WIPED', 'SQUAD WIPED'];
const ICON_STEP_S = 0.25;
const OUT_S = 0.25;

export const killLabel = (kills: number, label?: string): string =>
  (label && label.trim() ? label : DEFAULT_LABELS[clamp(kills, 1, 5) - 1]).toUpperCase();

export const KillCounter: React.FC<KillCounterProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const total = p.durationInFrames / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const portrait = p.height > p.width;
  const pixel = p.style === 'pixel';
  const text = killLabel(p.kills, p.label);

  // Dimensions : icônes (8 px logiques de 2 unités) + libellé ajusté à la largeur disponible.
  const iconPx = unit * 2;
  const iconW = iconPx * 8;
  const gap = unit * 2;
  const rowW = p.kills * iconW + (p.kills - 1) * gap;
  const maxW = portrait ? box.width : Math.round(p.width * 0.42);
  const padX = unit * 4;
  const padY = unit * 3;
  const fontSize = snapTo(
    clamp((maxW - padX * 2) / (text.length * (pixel ? 1 : 0.62)), unit * 3, unit * 9),
    unit,
  );
  const textW = Math.ceil(text.length * fontSize * (pixel ? 1 : 0.62));
  const panelW = snapTo(Math.min(maxW, Math.max(rowW, textW) + padX * 2), unit);
  const panelH = padY * 2 + fontSize + unit * 3 + iconW;

  const left = portrait
    ? snapTo(box.left + (box.width - panelW) / 2, unit)
    : snapTo(p.width - Math.round(p.width * 0.05) - panelW - unit * 3, unit);
  const top = box.top;

  // Allumage d'une icône toutes les 0,25 s ; la dernière déclenche flash + shake.
  const lit = clamp(Math.floor(t / ICON_STEP_S) + 1, 0, p.kills);
  const finalAt = (p.kills - 1) * ICON_STEP_S;
  const since = t - finalAt;
  const flashing = since >= 0 && since < 0.2;
  const flashOn = flashing && Math.floor(since * p.fps) % 2 === 0;
  const shaking = since >= 0 && since < 0.4;
  const shakeStep = Math.floor(frame / 2);
  const amp = unit * (since < 0.2 ? 2 : 1);
  const shakeX = shaking ? Math.round((hash(shakeStep) - 0.5) * 2) * amp : 0;
  const shakeY = shaking ? Math.round((hash(shakeStep + 99) - 0.5) * 2) * amp : 0;

  const remaining = total - t;
  const outStep = remaining < OUT_S ? Math.floor((1 - remaining / OUT_S) * 3) : 0;
  const outScale = [1, 0.75, 0.5][clamp(outStep, 0, 2)];
  const scale = popScale(t, 0) * beatScale(t, p.beats) * outScale;
  const labelColor = flashOn ? '#000000' : lit >= p.kills ? p.theme.accent : '#FFFFFF';

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div
        style={{
          position: 'absolute',
          left: left + shakeX,
          top: top + shakeY,
          width: panelW,
          height: panelH,
          boxSizing: 'border-box',
          padding: `${padY}px ${padX}px`,
          background: flashOn ? '#FFFFFF' : 'rgba(0,0,0,0.78)',
          border: `${unit}px solid ${flashOn ? p.theme.accent : p.theme.primary}`,
          boxShadow: `${unit * 2}px ${unit * 2}px 0 0 rgba(0,0,0,0.55), inset 0 ${unit}px 0 0 rgba(255,255,255,0.18)`,
          fontFamily: fontFor(p.style),
          textAlign: 'center',
          transform: `scale(${scale})`,
          transformOrigin: portrait ? '50% 0%' : '100% 0%',
        }}
      >
        <div
          style={{
            fontSize,
            lineHeight: `${fontSize}px`,
            height: fontSize,
            whiteSpace: 'nowrap',
            color: labelColor,
            textShadow: flashOn ? undefined : blockShadow(unit, p.theme.primary, null).split(', ')[0],
          }}
        >
          {text}
        </div>
        <div style={{display: 'flex', justifyContent: 'center', gap, marginTop: unit * 3}}>
          {Array.from({length: p.kills}, (_, i) => {
            const on = i < lit;
            const justLit = on && i === lit - 1 && t - i * ICON_STEP_S < 0.1;
            return (
              <div
                key={i}
                style={{
                  transform: `scale(${on ? popScale(t, i * ICON_STEP_S) : 1})`,
                  opacity: on ? 1 : 0.3,
                }}
              >
                <PixelSkull
                  px={iconPx}
                  color={on ? (justLit || flashOn ? '#FFFFFF' : p.theme.accent) : '#6B6B6B'}
                  hole={on ? p.theme.primary : '#2A2A2A'}
                  outline={on ? '#000000' : undefined}
                />
              </div>
            );
          })}
        </div>
      </div>
      {flashOn ? (
        <div style={{position: 'absolute', inset: 0, background: 'rgba(255,255,255,0.18)'}} />
      ) : null}
    </Stage>
  );
};
