// Agent Valley Configuration Constants

export const CHAR_BASE = '/agent-valley/character_assets/';
export const MAP_BASE = '/agent-valley/Map-opensorce/';
export const UI_BASE = '/agent-valley/UI/';

export const USE_AGENT_TOWN_MOCK = true;

// Map configuration - matching xsafe
export const DEFAULT_MAP_CONFIG = {
  id: 'map1',
  label: 'Classic',
  bundled: true,
  description: 'Office floor with agent visualization',
  mapUrl: `${MAP_BASE}Map_opensource.tmj`,
  visualLayer: 'Map1',
  tilesetName: 'Map1',
  imageAsset: `${MAP_BASE}Map1.png`,
  renderMode: 'whole-image', // Use whole image rendering like xsafe
};

export const MAP_VARIANTS = [
  DEFAULT_MAP_CONFIG,
];

export const MUSIC_TRACKS = [
  { id: 'ambient', name: 'Ambient', url: '/agent-valley/music/ambient.mp3' },
];

// Frame dimensions - matching xsafe
export const FW = 32;
export const FH = 64;

// NPC display scale - matching xsafe
export const NPC_SCALE = 3.9;

// Background color - matching xsafe
export const BG_COLOR = '#1a1a2e';

// Scene dimensions (will be calculated from map)
export const SCENE_W = 896;
export const SCENE_H = 640;

// Agent status types
export type AgentStatus = 'idle' | 'working' | 'pending' | 'offline';

// Agent character names - matching actual file names (capitalized)
export const AGENT_CHARACTERS = [
  'Adam', 'Alex', 'Amelia', 'Bob', 'Lucy',
];

export function getRandomCharacter(): string {
  return AGENT_CHARACTERS[Math.floor(Math.random() * AGENT_CHARACTERS.length)];
}

export function removeDemoSession(sessionKey: string): void {
  console.log('[removeDemoSession]', sessionKey);
}
