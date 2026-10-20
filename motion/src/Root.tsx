import React from 'react';
import {Composition, Still} from 'remotion';
import type {CalculateMetadataFunction} from 'remotion';
import {Captions, captionsSchema} from './templates/Captions';
import {Title, titleSchema} from './templates/Title';
import {Popup, popupSchema} from './templates/Popup';
import {ProgressBar, progressBarSchema} from './templates/ProgressBar';
import {Reaction, reactionSchema} from './templates/Reaction';
import {KillCounter, killCounterSchema} from './templates/KillCounter';
import {Rewind, rewindSchema} from './templates/Rewind';
import {RankCard, rankCardSchema} from './templates/RankCard';
import {Thumbnail, thumbnailSchema} from './templates/Thumbnail';

// GIF 1x1 : image de remplacement pour le Studio (le bridge Python fournit de vraies images).
const PLACEHOLDER =
  'data:image/gif;base64,R0lGODlhAQABAIAAAMzMzAAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==';

type Base = {width: number; height: number; fps: number; durationInFrames: number};

// Dimensions, fps et durée viennent des props (1920x1080 ou 1080x1920, durée de l'overlay).
const metadata = <T extends Base>(): CalculateMetadata<T> => async ({props}) => ({
  width: props.width,
  height: props.height,
  fps: props.fps,
  durationInFrames: props.durationInFrames,
});
type CalculateMetadata<T extends Record<string, unknown>> = CalculateMetadataFunction<T>;

const common = {width: 1080, height: 1920, fps: 30, durationInFrames: 90};

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="captions"
      component={Captions}
      schema={captionsSchema}
      {...common}
      defaultProps={captionsSchema.parse({
        words: [
          {text: 'NEW', start: 0, end: 0.4},
          {text: 'HIGH', start: 0.4, end: 0.8},
          {text: 'SCORE', start: 0.8, end: 1.4},
        ],
      })}
      calculateMetadata={metadata()}
    />
    <Composition
      id="title"
      component={Title}
      schema={titleSchema}
      {...common}
      defaultProps={titleSchema.parse({subtitle: 'insert coin'})}
      calculateMetadata={metadata()}
    />
    <Composition
      id="popup"
      component={Popup}
      schema={popupSchema}
      {...common}
      defaultProps={popupSchema.parse({})}
      calculateMetadata={metadata()}
    />
    <Composition
      id="progress-bar"
      component={ProgressBar}
      schema={progressBarSchema}
      {...common}
      defaultProps={progressBarSchema.parse({label: 'hp'})}
      calculateMetadata={metadata()}
    />
    <Composition
      id="reaction"
      component={Reaction}
      schema={reactionSchema}
      {...common}
      defaultProps={reactionSchema.parse({})}
      calculateMetadata={metadata()}
    />
    <Composition
      id="kill-counter"
      component={KillCounter}
      schema={killCounterSchema}
      {...common}
      durationInFrames={60}
      defaultProps={killCounterSchema.parse({kills: 3})}
      calculateMetadata={metadata()}
    />
    <Composition
      id="rewind"
      component={Rewind}
      schema={rewindSchema}
      {...common}
      durationInFrames={24}
      defaultProps={rewindSchema.parse({})}
      calculateMetadata={metadata()}
    />
    <Composition
      id="rank-card"
      component={RankCard}
      schema={rankCardSchema}
      {...common}
      durationInFrames={54}
      defaultProps={rankCardSchema.parse({rank: 3, total: 10, label: 'best fails'})}
      calculateMetadata={metadata()}
    />
    <Still
      id="thumbnail"
      component={Thumbnail}
      schema={thumbnailSchema}
      width={1280}
      height={720}
      defaultProps={thumbnailSchema.parse({images: [PLACEHOLDER], text: 'GG EZ'})}
      calculateMetadata={async ({props}) => ({width: props.width, height: props.height})}
    />
  </>
);
