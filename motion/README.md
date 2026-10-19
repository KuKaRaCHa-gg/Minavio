# motion/ — templates Remotion (pixel / arcade)

Projet Remotion 4 (React/TS). Fonte *Press Start 2P* via `@remotion/google-fonts`, rendu net,
animations en paliers. Compositions : `captions`, `title`, `popup`, `progress-bar` (template Python
`progress_bar`), `reaction`. Props communes : `width`, `height`, `fps`, `durationInFrames`,
`theme {primary, accent}`, `beats` (s, relatifs à l'overlay), `scanlines`, `style` (`pixel`|`clean`).

```
npm install
npx tsc --noEmit
npx remotion studio src/index.ts
npx remotion still src/index.ts captions out.png --props=props.json
```

Le rendu de production passe par `minavio.motion.render.render_overlays` (ProRes 4444 alpha).
Sans accès à fonts.gstatic.com, `loadFont` échoue : repli `monospace`.
Navigateur personnalisé : variable `MINAVIO_BROWSER_EXECUTABLE`.
