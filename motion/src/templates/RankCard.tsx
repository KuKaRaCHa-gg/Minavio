import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape} from '../lib/schema';
import {
  Stage,
  beatScale,
  blockShadow,
  clamp,
  fontFor,
  pixelUnit,
  popScale,
  safeBox,
  snapTo,
  stepped,
} from '../lib/pixel';

export const rankCardSchema = z.object({
  ...baseShape,
  rank: z.number().int().min(1).default(1),
  total: z.number().int().min(1).default(10),
  label: z.string().optional(),
  durationInFrames: z.number().int().positive().default(54),
});
export type RankCardProps = z.infer<typeof rankCardSchema>;

const IN_S = 0.3;
const OUT_S = 0.25;

export const RankCard: React.FC<RankCardProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const total = p.durationInFrames / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const portrait = p.height > p.width;
  const pixel = p.style === 'pixel';
  const rank = p.rank;
  const count = Math.max(p.total, rank);
  const numText = `#${rank}`;
  const label = (p.label ?? '').toUpperCase();

  const cardW = snapTo(portrait ? box.width * 0.9 : Math.min(p.width * 0.34, 640), unit);
  const padX = unit * 5;
  const padY = unit * 4;
  const inner = cardW - padX * 2 - unit * 2;
  const numSize = snapTo(clamp(inner / (numText.length * (pixel ? 1 : 0.62)), unit * 8, unit * 30), unit);
  const labelSize = snapTo(
    clamp(inner / Math.max(1, label.length * (pixel ? 1 : 0.62)), unit * 3, unit * 6),
    unit,
  );
  const headSize = unit * 3;
  const cells = clamp(count, 1, 20);
  const filled = clamp(Math.max(1, Math.round((rank / count) * cells)), 1, cells);
  const barH = unit * 5;
  const cardH =
    padY * 2 + headSize + unit * 4 + numSize + (label ? unit * 4 + labelSize : 0) + unit * 5 + barH;

  const left = portrait ? snapTo(box.left + (box.width - cardW) / 2, unit) : snapTo(box.left, unit);
  const top = portrait ? box.top + unit * 4 : snapTo(box.top + (box.height - cardH) / 2, unit);

  // Glissement par paliers : entrée 6 paliers depuis la gauche, sortie 4 paliers.
  const slideDist = left + cardW + unit * 4;
  const enter = stepped(t / IN_S, 6);
  const remaining = total - t;
  const exit = remaining < OUT_S ? stepped(1 - remaining / OUT_S, 4) : 0;
  const x = -slideDist * (1 - enter) - slideDist * exit;

  // Remplissage de la barre : une cellule toutes les ~0,04 s après l'entrée.
  const shown = clamp(Math.floor((t - IN_S) / 0.04) + 1, 0, filled);
  const numScale = popScale(t, IN_S) * beatScale(t, p.beats);
  const gold = rank === 1;
  const blink = gold && Math.floor(t * 6) % 2 === 1;

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div
        style={{
          position: 'absolute',
          left,
          top,
          width: cardW,
          height: cardH,
          boxSizing: 'border-box',
          padding: `${padY}px ${padX}px`,
          transform: `translateX(${x}px)`,
          background: 'rgba(8,6,20,0.88)',
          border: `${unit}px solid ${p.theme.primary}`,
          boxShadow: `${unit * 2}px ${unit * 2}px 0 0 rgba(0,0,0,0.6), inset 0 0 0 ${unit}px #000, inset 0 ${unit * 2}px 0 0 rgba(255,255,255,0.1)`,
          fontFamily: fontFor(p.style),
          textAlign: 'center',
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: headSize,
            lineHeight: `${headSize}px`,
            color: p.theme.accent,
          }}
        >
          <span>RANK</span>
          <span>
            {rank}/{count}
          </span>
        </div>
        <div
          style={{
            marginTop: unit * 4,
            height: numSize,
            fontSize: numSize,
            lineHeight: `${numSize}px`,
            whiteSpace: 'nowrap',
            color: blink ? '#FFFFFF' : gold ? p.theme.accent : '#FFFFFF',
            textShadow: blockShadow(unit * 2, p.theme.primary, '#000000'),
            transform: `scale(${numScale})`,
            transformOrigin: '50% 50%',
          }}
        >
          {numText}
        </div>
        {label ? (
          <div
            style={{
              marginTop: unit * 4,
              fontSize: labelSize,
              lineHeight: `${labelSize}px`,
              height: labelSize,
              whiteSpace: 'nowrap',
              color: '#FFFFFF',
              textShadow: blockShadow(unit, '#000000', null).split(', ')[0],
            }}
          >
            {label}
          </div>
        ) : null}
        <div style={{display: 'flex', gap: unit, marginTop: unit * 5, height: barH}}>
          {Array.from({length: cells}, (_, i) => (
            <div
              key={i}
              style={{
                flex: 1,
                background:
                  i < shown ? (i === filled - 1 ? p.theme.accent : p.theme.primary) : 'rgba(255,255,255,0.14)',
                boxShadow: i < shown ? `inset 0 ${unit}px 0 0 rgba(255,255,255,0.4)` : undefined,
              }}
            />
          ))}
        </div>
      </div>
    </Stage>
  );
};
