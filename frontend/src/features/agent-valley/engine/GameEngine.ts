import * as PIXI from 'pixi.js';
import SpriteLoader from './SpriteLoader';
import TiledRenderer from './TiledRenderer';
import type { Agent, AgentData, AgentEvent } from '../types';
import { BG_COLOR, NPC_SCALE, FH } from '../config/constants';

interface NPCSprite {
  id: string;
  agent: Agent;
  sprite: PIXI.AnimatedSprite;
  shadow: PIXI.Graphics;
  container: PIXI.Container;
  charName: string;
  currentAnim: 'idle' | 'run' | 'phone';
  speed: number;
  // For idle walking behavior
  targetY?: number;
  walkDirection?: 1 | -1; // 1 = down, -1 = up
  minY?: number;
  maxY?: number;
  // For exclamation mark
  exclamationMark?: PIXI.Sprite | null;
  showExclamation?: boolean;
}

export default class GameEngine {
  app: PIXI.Application | null = null;
  world: PIXI.Container | null = null;
  npcLayer: PIXI.Container | null = null;
  spriteLoader: SpriteLoader;
  tiledRenderer: TiledRenderer | null = null;
  npcs: NPCSprite[] = [];
  sceneW = 896;
  sceneH = 640;
  mapConfig: any;
  private _issueQuestionTexture: PIXI.Texture | null = null; // Status mark texture

  onNpcHover: ((data: AgentData, pos: { x: number; y: number }) => void) | null = null;
  onNpcLeave: (() => void) | null = null;
  onNpcClick: ((data: AgentData) => void) | null = null;
  onCursorStateChange: ((state: string) => void) | null = null;
  onLayoutChange: ((layout: { sceneW: number; sceneH: number }) => void) | null = null;

  private _containerEl: HTMLElement | null = null;
  private _resizeObs: ResizeObserver | null = null;
  private _agentsById: Map<string, Agent> = new Map();
  private _eventsByAgent: Map<string, AgentEvent[]> = new Map();
  private _gameLoopStarted = false;

  constructor(options: any = {}) {
    this.spriteLoader = new SpriteLoader();
    this.mapConfig = options.mapConfig || {};
  }

  init(containerEl: HTMLElement): this {
    console.log('[GameEngine] Initializing...');
    PIXI.BaseTexture.defaultOptions.scaleMode = PIXI.SCALE_MODES.NEAREST;

    this.app = new PIXI.Application({
      background: BG_COLOR,
      antialias: false,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    });

    containerEl.appendChild(this.app.view as HTMLCanvasElement);
    (this.app.view as HTMLCanvasElement).style.width = '100%';
    (this.app.view as HTMLCanvasElement).style.height = '100%';
    (this.app.view as HTMLCanvasElement).style.touchAction = 'none';

    this.world = new PIXI.Container();
    this.world.sortableChildren = true;
    this.app.stage.addChild(this.world);

    this.npcLayer = new PIXI.Container();
    this.npcLayer.sortableChildren = true;
    this.npcLayer.zIndex = 100;

    this._containerEl = containerEl;
    this._resizeObs = new ResizeObserver(() => this._resize());
    this._resizeObs.observe(containerEl);

    console.log('[GameEngine] Initialized successfully');
    return this;
  }

  private _resize(): void {
    if (!this.app || !this._containerEl) return;
    const w = this._containerEl.clientWidth;
    const h = this._containerEl.clientHeight;
    if (w < 1 || h < 1) return;

    this.app.renderer.resize(w, h);

    if (this.world) {
      const scale = Math.min(w / this.sceneW, h / this.sceneH);
      this.world.scale.set(scale);
      this.world.position.set(
        (w - this.sceneW * scale) / 2,
        (h - this.sceneH * scale) / 2
      );
    }
  }

  private _setupInteraction(): void {
    if (!this.app || !this.npcLayer) return;

    this.npcLayer.eventMode = 'static';
    this.npcLayer.hitArea = new PIXI.Rectangle(0, 0, this.sceneW, this.sceneH);

    this.npcLayer.on('pointermove', (event: PIXI.FederatedPointerEvent) => {
      const pos = event.global;
      const localPos = this.npcLayer!.toLocal(pos);

      let hoveredNpc: NPCSprite | null = null;
      for (const npc of this.npcs) {
        const dx = localPos.x - npc.container.x;
        const dy = localPos.y - npc.container.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        if (distance <= 50) {
          hoveredNpc = npc;
          break;
        }
      }

      if (hoveredNpc) {
        const agentData: AgentData = {
          agent: hoveredNpc.agent,
          charName: hoveredNpc.charName,
          state: hoveredNpc.agent.status,
        };
        this.onNpcHover?.(agentData, { x: pos.x, y: pos.y });
        if (this.app?.view) {
          (this.app.view as HTMLCanvasElement).style.cursor = 'pointer';
        }
      } else {
        this.onNpcLeave?.();
        if (this.app?.view) {
          (this.app.view as HTMLCanvasElement).style.cursor = 'default';
        }
      }
    });

    this.npcLayer.on('pointerdown', (event: PIXI.FederatedPointerEvent) => {
      const pos = event.global;
      const localPos = this.npcLayer!.toLocal(pos);
      console.log('[GameEngine] Click at:', localPos.x, localPos.y, 'NPCs:', this.npcs.length);

      for (const npc of this.npcs) {
        const dx = localPos.x - npc.container.x;
        const dy = localPos.y - npc.container.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        console.log('[GameEngine] Distance to', npc.agent.name, ':', distance, 'dx:', dx, 'dy:', dy);
        // Clickable area: within 30 pixels radius
        if (distance <= 30) {
          console.log('[GameEngine] Clicked on agent:', npc.agent.name);
          const events = this._eventsByAgent.get(npc.agent.id) || [];
          const agentData: AgentData = {
            agent: npc.agent,
            charName: npc.charName,
            state: npc.agent.status,
            events,
          };
          console.log('[GameEngine] Calling onNpcClick with data:', agentData);
          this.onNpcClick?.(agentData);
          break;
        }
      }
    });
  }

  async loadAssets(onProgress?: (progress: number, label?: string) => void): Promise<void> {
    console.log('[GameEngine] Loading assets...');

    if (!this.world) {
      console.error('[GameEngine] World not initialized!');
      return;
    }

    // Load map
    if (this.mapConfig.mapUrl) {
      onProgress?.(0.1, 'Loading map...');
      try {
        console.log('[GameEngine] Fetching map from:', this.mapConfig.mapUrl);
        const mapRes = await fetch(this.mapConfig.mapUrl);
        const mapData = await mapRes.json();
        console.log('[GameEngine] Map data loaded, size:', mapData.width, 'x', mapData.height);

        this.tiledRenderer = new TiledRenderer(mapData, {
          visualLayerName: this.mapConfig.visualLayer,
          tilesetName: this.mapConfig.tilesetName,
          imageAsset: this.mapConfig.imageAsset,
          renderMode: this.mapConfig.renderMode,
        });

        await this.tiledRenderer.loadTilesets('', this.mapConfig.mapUrl);
        console.log('[GameEngine] Tilesets loaded');

        // Check if world still exists after async operation
        if (!this.world) {
          console.warn('[GameEngine] World was destroyed during tileset loading');
          return;
        }

        const mapContainer = this.tiledRenderer.render();
        mapContainer.zIndex = 0;
        this.world.addChild(mapContainer);
        console.log('[GameEngine] Map container added to world');

        const bounds = this.tiledRenderer.getContentBounds();
        this.sceneW = bounds.pixelW;
        this.sceneH = bounds.pixelH;
        console.log('[GameEngine] Scene size:', this.sceneW, 'x', this.sceneH);

        onProgress?.(0.3, 'Map loaded');
      } catch (e) {
        console.error('[GameEngine] Failed to load map:', e);
        onProgress?.(0.3, 'Map load failed, continuing...');
      }
    }

    // Check if world still exists
    if (!this.world) {
      console.warn('[GameEngine] World was destroyed during map loading');
      return;
    }

    // Add NPC layer to world
    this.world.addChild(this.npcLayer!);
    console.log('[GameEngine] NPC layer added to world');

    // Load character sprites
    onProgress?.(0.4, 'Loading character sprites...');
    // Use character names that actually exist in the assets (capitalized)
    const charNames = ['Alex', 'Bob', 'Lucy', 'Adam', 'Amelia'];
    await this.spriteLoader.load(charNames, '/agent-valley/character_assets/', (p) => {
      onProgress?.(0.4 + p * 0.5, 'Loading character sprites...');
    });
    console.log('[GameEngine] Character sprites loaded');

    // Load status mark texture (exclamation mark)
    try {
      const markTexture = await PIXI.Assets.load('/agent-valley/UI/png/status/marks.png');
      if (markTexture?.baseTexture) {
        markTexture.baseTexture.scaleMode = PIXI.SCALE_MODES.NEAREST;
      }
      this._issueQuestionTexture = markTexture;
      console.log('[GameEngine] Status mark texture loaded');
    } catch (e) {
      console.error('[GameEngine] Failed to load status mark texture:', e);
      this._issueQuestionTexture = null;
    }

    // Check if world still exists after sprite loading
    if (!this.world) {
      console.warn('[GameEngine] World was destroyed during sprite loading');
      return;
    }

    this._setupInteraction();

    // Start game loop for idle agent movement
    this._startGameLoop();

    this.onLayoutChange?.({ sceneW: this.sceneW, sceneH: this.sceneH });
    onProgress?.(1, 'Ready!');
    console.log('[GameEngine] Assets loaded successfully');
  }

  populateNPCs(agents: Agent[], events: AgentEvent[]): void {
    if (!this.npcLayer) return;

    this.npcs.forEach(npc => {
      this.npcLayer!.removeChild(npc.container);
    });
    this.npcs = [];
    this._agentsById.clear();
    this._eventsByAgent.clear();

    events.forEach(event => {
      const list = this._eventsByAgent.get(event.agent_id) || [];
      list.push(event);
      this._eventsByAgent.set(event.agent_id, list);
    });

    agents.forEach(agent => {
      this._agentsById.set(agent.id, agent);
      const npc = this._createNPC(agent);

      // Set up idle walking behavior for idle agents
      if (npc && agent.status === 'idle') {
        npc.minY = 500;
        npc.maxY = 800;
        npc.targetY = npc.maxY;
        npc.walkDirection = 1;
        console.log('[GameEngine] Set up idle walking for agent:', agent.name, 'between y:', npc.minY, '-', npc.maxY);
      }
    });

    this.onLayoutChange?.({ sceneW: this.sceneW, sceneH: this.sceneH });
  }

  private _createNPC(agent: Agent): NPCSprite | undefined {
    if (!this.npcLayer) return;

    const charName = agent.charName;
    const frames = this.spriteLoader.charFrames[charName];
    if (!frames) {
      console.warn('No frames for character:', charName);
      return;
    }

    let animFrames: PIXI.Texture[];
    let animSpeed = 0.15;

    if (agent.status === 'working') {
      if (frames.phone.length > 0) {
        animFrames = frames.phone;
        animSpeed = 0.12;
      } else {
        animFrames = frames.right;
        animSpeed = 0.18;
      }
    } else {
      animFrames = frames.idle;
      animSpeed = 0.1;
    }

    const sprite = new PIXI.AnimatedSprite(animFrames);
    sprite.animationSpeed = animSpeed;
    sprite.play();
    sprite.anchor.set(0.5, 1);
    sprite.scale.set(NPC_SCALE);

    const shadow = new PIXI.Graphics();
    shadow.beginFill(0x000000, 0.3);
    shadow.drawEllipse(0, 0, 12 * NPC_SCALE, 6 * NPC_SCALE);
    shadow.endFill();
    shadow.y = 4;

    // Create exclamation mark using image (similar to xsafe style)
    let exclamationMark: PIXI.Sprite | null = null;
    if (this._issueQuestionTexture) {
      exclamationMark = new PIXI.Sprite(this._issueQuestionTexture);
      exclamationMark.anchor.set(0.5, 1);
      exclamationMark.scale.set(0.35); // Slightly larger size
      exclamationMark.x = 0;
      exclamationMark.y = -FH * NPC_SCALE - 5; // Closer to character
      exclamationMark.alpha = 0.98;
      exclamationMark.tint = 0xff4444; // Red tint for notification
      exclamationMark.visible = false;
      console.log('[GameEngine] Created exclamation mark sprite for:', agent.name);
    } else {
      console.warn('[GameEngine] No status mark texture available for:', agent.name);
    }

    const container = new PIXI.Container();
    container.addChild(shadow);
    container.addChild(sprite);
    if (exclamationMark) {
      container.addChild(exclamationMark);
    }
    container.sortableChildren = true;

    const x = agent.position?.x || Math.random() * this.sceneW;
    const y = agent.position?.y || Math.random() * this.sceneH;
    container.position.set(x, y);
    container.zIndex = y;

    this.npcLayer.addChild(container);

    const npcSprite: NPCSprite = {
      id: agent.id,
      agent,
      sprite,
      shadow,
      container,
      charName,
      currentAnim: agent.status === 'working' ? 'phone' : 'idle',
      speed: 1.5,
      exclamationMark,
      showExclamation: false,
    };

    this.npcs.push(npcSprite);
    return npcSprite;
  }

  updateData(agents: Agent[], events: AgentEvent[]): void {
    this._eventsByAgent.clear();
    events.forEach(event => {
      const list = this._eventsByAgent.get(event.agent_id) || [];
      list.push(event);
      this._eventsByAgent.set(event.agent_id, list);
    });

    const agentMap = new Map(agents.map(a => [a.id, a]));

    this.npcs = this.npcs.filter(npc => {
      if (!agentMap.has(npc.id)) {
        this.npcLayer!.removeChild(npc.container);
        return false;
      }
      return true;
    });

    agents.forEach(agent => {
      const existing = this.npcs.find(n => n.id === agent.id);
      if (existing) {
        existing.agent = agent;

        // Update idle walking behavior based on status
        if (agent.status === 'idle') {
          if (existing.minY === undefined) {
            existing.minY = 500;
            existing.maxY = 800;
            existing.targetY = existing.maxY;
            existing.walkDirection = 1;
            console.log('[GameEngine] Enabled idle walking for agent:', agent.name);
          }
        } else {
          // Disable idle walking for non-idle agents
          if (existing.minY !== undefined) {
            existing.minY = undefined;
            existing.maxY = undefined;
            existing.targetY = undefined;
            existing.walkDirection = undefined;
            console.log('[GameEngine] Disabled idle walking for agent:', agent.name);
          }
        }

        this._updateNPCAnimation(existing);
      } else {
        const npc = this._createNPC(agent);

        // Set up idle walking behavior for idle agents
        if (npc && agent.status === 'idle') {
          npc.minY = 500;
          npc.maxY = 800;
          npc.targetY = npc.maxY;
          npc.walkDirection = 1;
          console.log('[GameEngine] Set up idle walking for new agent:', agent.name);
        }
      }
    });

    this._agentsById = agentMap;
  }

  private _updateNPCAnimation(npc: NPCSprite): void {
    const agent = npc.agent;
    const frames = this.spriteLoader.charFrames[npc.charName];
    if (!frames) {
      console.error('[GameEngine] No frames found for character:', npc.charName);
      return;
    }

    let newAnim: 'idle' | 'run' | 'phone' = 'idle';
    let animFrames: PIXI.Texture[];
    let animSpeed = 0.15;

    console.log('[GameEngine] _updateNPCAnimation - agent status:', agent.status, 'current anim:', npc.currentAnim);
    console.log('[GameEngine] Available animations:', {
      idle: frames.idle?.length || 0,
      right: frames.right?.length || 0,
      phone: frames.phone?.length || 0,
    });

    if (agent.status === 'working') {
      // Working: use phone animation
      if (frames.phone && frames.phone.length > 0) {
        newAnim = 'phone';
        animFrames = frames.phone;
        animSpeed = 0.12;
        console.log('[GameEngine] ✅ Setting animation to phone (working), frames count:', frames.phone.length);
      } else {
        // Fallback to idle if no phone animation
        newAnim = 'idle';
        animFrames = frames.idle;
        animSpeed = 0.1;
        console.log('[GameEngine] ⚠️ No phone animation, fallback to idle');
      }
    } else {
      // Idle: use run animation for walking
      if (frames.right && frames.right.length > 0) {
        newAnim = 'run';
        animFrames = frames.right;
        animSpeed = 0.18;
        console.log('[GameEngine] ✅ Setting animation to run (idle/walking), frames count:', frames.right.length);
      } else {
        // Fallback to idle if no run animation
        newAnim = 'idle';
        animFrames = frames.idle;
        animSpeed = 0.1;
        console.log('[GameEngine] ⚠️ No run animation, fallback to idle');
      }
    }

    if (npc.currentAnim !== newAnim) {
      console.log('[GameEngine] 🔄 Switching animation from', npc.currentAnim, 'to', newAnim);
      console.log('[GameEngine] Animation details:', {
        oldAnim: npc.currentAnim,
        newAnim: newAnim,
        framesCount: animFrames.length,
        animSpeed: animSpeed,
        isPlaying: npc.sprite.playing,
      });

      npc.currentAnim = newAnim;

      // Stop current animation first
      npc.sprite.stop();

      // Set new textures
      npc.sprite.textures = animFrames;
      npc.sprite.animationSpeed = animSpeed;

      // Start from first frame
      npc.sprite.gotoAndPlay(0);

      console.log('[GameEngine] ✅ Animation switched, now playing:', npc.sprite.playing, 'current frame:', npc.sprite.currentFrame);
    } else {
      console.log('[GameEngine] Animation already set to', newAnim);

      // Even if animation is the same, ensure it's playing
      if (!npc.sprite.playing) {
        console.log('[GameEngine] ⚠️ Animation not playing, restarting...');
        npc.sprite.gotoAndPlay(0);
      }
    }
  }

  deleteAgentById(agentId: string): void {
    const index = this.npcs.findIndex(n => n.id === agentId);
    if (index >= 0) {
      const npc = this.npcs[index];
      this.npcLayer!.removeChild(npc.container);
      this.npcs.splice(index, 1);
      this._agentsById.delete(agentId);
    }
  }

  showExclamationMark(agentId: string, show: boolean): void {
    console.log('[GameEngine] showExclamationMark called:', { agentId, show, npcsCount: this.npcs.length });

    const npc = this.npcs.find(n => n.id === agentId);

    if (!npc) {
      console.error('[GameEngine] ❌ NPC not found for agentId:', agentId);
      return;
    }

    console.log('[GameEngine] NPC found:', {
      id: npc.id,
      name: npc.agent.name,
      hasExclamationMark: !!npc.exclamationMark,
    });

    if (!npc.exclamationMark) {
      console.error('[GameEngine] ❌ Exclamation mark not found on NPC:', npc.id);
      return;
    }

    console.log('[GameEngine] Exclamation mark details:', {
      visible: npc.exclamationMark.visible,
      x: npc.exclamationMark.x,
      y: npc.exclamationMark.y,
      text: npc.exclamationMark.text,
      alpha: npc.exclamationMark.alpha,
    });

    npc.exclamationMark.visible = show;
    npc.showExclamation = show;

    console.log('[GameEngine] ✅ Exclamation mark', show ? 'shown' : 'hidden', 'for agent:', agentId);
    console.log('[GameEngine] After update - visible:', npc.exclamationMark.visible);
  }

  destroy(): void {
    console.log('[GameEngine] Destroying...');
    this._gameLoopStarted = false;

    if (this._resizeObs) {
      this._resizeObs.disconnect();
      this._resizeObs = null;
    }

    this.npcs.forEach(npc => {
      npc.sprite.destroy();
      npc.container.destroy();
    });
    this.npcs = [];

    if (this.app) {
      this.app.destroy(true, { children: true, texture: true, baseTexture: true });
      this.app = null;
    }

    this.world = null;
    this.npcLayer = null;
    this._containerEl = null;
    console.log('[GameEngine] Destroyed');
  }

  private _startGameLoop(): void {
    if (!this.app || this._gameLoopStarted) return;
    this._gameLoopStarted = true;

    console.log('[GameEngine] Starting game loop for idle agent movement');

    this.app.ticker.add(() => {
      if (!this._gameLoopStarted) return;

      // Update each NPC
      this.npcs.forEach(npc => {
        // Only move idle agents
        const shouldMove = npc.agent.status === 'idle' && npc.minY !== undefined && npc.maxY !== undefined;

        if (shouldMove) {
          this._updateIdleMovement(npc);
        }
      });
    });
  }

  private _updateIdleMovement(npc: NPCSprite): void {
    const speed = 1.5; // pixels per frame

    // Initialize target if not set
    if (npc.targetY === undefined) {
      npc.targetY = npc.maxY;
      npc.walkDirection = 1; // Start moving down
    }

    const currentY = npc.container.y;
    const distanceToTarget = Math.abs(currentY - npc.targetY!);

    // If reached target, switch direction
    if (distanceToTarget < speed) {
      if (npc.walkDirection === 1) {
        // Was moving down, now move up
        npc.targetY = npc.minY;
        npc.walkDirection = -1;
      } else {
        // Was moving up, now move down
        npc.targetY = npc.maxY;
        npc.walkDirection = 1;
      }
    }

    // Move towards target
    if (currentY < npc.targetY!) {
      npc.container.y += speed;
      npc.sprite.scale.x = Math.abs(npc.sprite.scale.x); // Face right/down
    } else if (currentY > npc.targetY!) {
      npc.container.y -= speed;
      npc.sprite.scale.x = -Math.abs(npc.sprite.scale.x); // Face left/up
    }

    // Update z-index for proper layering
    npc.container.zIndex = npc.container.y;
  }

  private _switchAnimation(npc: NPCSprite, anim: 'idle' | 'run' | 'phone'): void {
    if (npc.currentAnim === anim) return;

    const frames = this.spriteLoader.charFrames[npc.charName];
    if (!frames) return;

    let animFrames: PIXI.Texture[];
    let animSpeed = 0.15;

    if (anim === 'phone' && frames.phone.length > 0) {
      animFrames = frames.phone;
      animSpeed = 0.12;
    } else if (anim === 'run' && frames.right.length > 0) {
      animFrames = frames.right;
      animSpeed = 0.18;
    } else {
      animFrames = frames.idle;
      animSpeed = 0.1;
    }

    const oldScaleX = npc.sprite.scale.x;
    npc.sprite.textures = animFrames;
    npc.sprite.scale.x = oldScaleX; // Preserve facing direction
    npc.sprite.animationSpeed = animSpeed;
    npc.sprite.play();
    npc.currentAnim = anim;
  }
}
