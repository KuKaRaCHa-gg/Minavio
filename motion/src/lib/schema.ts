import {z} from 'zod';

export const themeSchema = z
  .object({
    primary: z.string().default('#FF3B30'),
    accent: z.string().default('#FFD60A'),
    font: z.string().optional(),
  })
  .passthrough();

export const baseShape = {
  width: z.number().int().positive().default(1080),
  height: z.number().int().positive().default(1920),
  fps: z.number().positive().default(30),
  durationInFrames: z.number().int().positive().default(90),
  theme: themeSchema.default({}),
  // Secondes, relatives au début de l'overlay.
  beats: z.array(z.number()).default([]),
  scanlines: z.boolean().default(false),
  style: z.enum(['pixel', 'clean']).default('pixel'),
};

export const positionSchema = z.enum(['top', 'center', 'bottom']);
export type Position = z.infer<typeof positionSchema>;
