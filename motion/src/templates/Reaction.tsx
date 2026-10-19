import React from 'react';
import {useCurrentFrame} from 'remotion';
import {z} from 'zod';
import {baseShape} from '../lib/schema';
import {Stage, beatScale, blockShadow, fontFor, pixelUnit, popScale, safeBox, snapTo} from '../lib/pixel';

export const reactionSchema = z.object({
  ...baseShape,
  // Sprite connu (skull, heart) ou texte libre ("!!!", "LOL", "GG"...).
  kind: z.string().default('!!!'),
  // Position centre du sprite, fractions de la zone sûre.
  x: z.number().min(0).max(1).default(0.5),
  y: z.number().min(0).max(1).default(0.35),
});
export type ReactionProps = z.infer<typeof reactionSchema>;

const SPRITES: Record<string, string[]> = {
  skull: [
    '..XXXXXX..',
    '.XXXXXXXX.',
    'XXXXXXXXXX',
    'XXXEXXEXXX',
    'XXEEXXEEXX',
    'XXXXXXXXXX',
    '.XXX..XXX.',
    '..XXXXXX..',
    '..X.XX.X..',
    '..XXXXXX..',
  ],
  heart: [
    '.XX..XX.',
    'XXXXXXXX',
    'XXXXXXXX',
    'XXXXXXXX',
    '.XXXXXX.',
    '..XXXX..',
    '...XX...',
  ],
};

const Sprite: React.FC<{rows: string[]; cell: number; color: string; eye: string}> = ({
  rows,
  cell,
  color,
  eye,
}) => (
  <svg
    width={rows[0].length * cell}
    height={rows.length * cell}
    shapeRendering="crispEdges"
    style={{display: 'block'}}
  >
    {rows.flatMap((row, y) =>
      row.split('').map((c, x) =>
        c === 'X' || c === 'E' ? (
          <rect
            key={`${x}-${y}`}
            x={x * cell}
            y={y * cell}
            width={cell}
            height={cell}
            fill={c === 'E' ? eye : color}
          />
        ) : null,
      ),
    )}
  </svg>
);

export const Reaction: React.FC<ReactionProps> = (p) => {
  const frame = useCurrentFrame();
  const t = frame / p.fps;
  const total = p.durationInFrames / p.fps;
  const unit = pixelUnit(p.width, p.height);
  const box = safeBox(p.width, p.height);
  const kind = p.kind.toLowerCase();
  const rows = SPRITES[kind];

  // Rebond en paliers : 4 positions par cycle, 2 cycles / seconde.
  const BOUNCE = [0, -3, -4, -3];
  const phase = Math.floor((t * 8) % 4);
  const dy = BOUNCE[phase] * unit * 2;
  const remaining = total - t;
  const blinkOff = remaining < 0.3 && Math.floor(remaining * 10) % 2 === 0;
  const scale = popScale(t, 0) * beatScale(t, p.beats);

  const cx = snapTo(box.left + box.width * p.x, unit);
  const cy = snapTo(box.top + box.height * p.y + dy, unit);

  let content: React.ReactNode;
  if (rows) {
    const cell = unit * (kind === 'skull' ? 3 : 4);
    content = (
      <div style={{filter: `drop-shadow(${unit * 2}px ${unit * 2}px 0 ${p.theme.primary})`}}>
        <Sprite rows={rows} cell={cell} color={kind === 'heart' ? p.theme.primary : '#FFFFFF'} eye="#000000" />
      </div>
    );
  } else {
    const label = p.kind.toUpperCase();
    const fontSize = snapTo(Math.min(unit * 14, (box.width * 0.9) / (label.length + 1)), unit);
    content = (
      <div
        style={{
          fontFamily: fontFor(p.style),
          fontSize,
          color: p.theme.accent,
          whiteSpace: 'nowrap',
          textShadow: blockShadow(unit, p.theme.primary, '#000000'),
        }}
      >
        {label}
      </div>
    );
  }

  return (
    <Stage unit={unit} scanlines={p.scanlines}>
      <div
        style={{
          position: 'absolute',
          left: cx,
          top: cy,
          opacity: blinkOff ? 0 : 1,
          transform: `translate(-50%, -50%) scale(${scale})`,
          transformOrigin: '50% 50%',
        }}
      >
        {content}
      </div>
    </Stage>
  );
};
