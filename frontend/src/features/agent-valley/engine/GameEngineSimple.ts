import * as PIXI from 'pixi.js';
import SpriteLoader, { CharFrames } from './SpriteLoader';
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
}

export default class GameEngine {
  app: PIXI.Application | null = null;
  world: PIXI.Container | null = null;
  npcLayer: PIXI.Container | null = null;
  spriteLoader: SpriteLoader;
  npcs: NPCSprite[] = [];
  sceneW = 896;
  sceneH = 640;

  onNpcHover: ((data: AgentData, pos: { x: number; y: number }) => void) | null = null;
  onNpcLeave: (() => void) | null = null;
  onNpcClick: ((data: AgentData) => void) | null = null;
  onCursorStateChange: ((state: string) => void) | null = null;
  onLayoutChange: ((layout: { sceneW: number; sceneH: number }) => void) | null = null;

  private _containerEl: HTMLElement | null = null;
  private _resizeObs: ResizeObserver | null = null;
  private _agentsById: Map<string, Agent> = new Map();
  private _eventsByAgent: Map<string, AgentEvent[]> = new Map();

  constructor() {
    this.spriteLoader = new SpriteLoader();
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
    this.world.addChild(this.npcLayer);

    this._containerEl = containerEl;
    this._resizeObs = new ResizeObserver(() => this._resize());
    this._resizeObs.observe(containerEl);

    this._setupInteraction();

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
        if (distance <= 32) {
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

      for (const npc of this.npcs) {
        const dx = localPos.x - npc.container.x;
        const dy = localPos.y - npc.container.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        if (distance <= 32) {
          const events = this._eventsByAgent.get(npc.agent.id) || [];
          const agentData: AgentData = {
            agent: npc.agent,
            charName: npc.charName,
            state: npc.agent.status,
            events,
          };
          this.onNpcClick?.(agentData);
          break;
        }
      }
    });
  }

  async loadAssets(onProgress?: (progress: number, label?: string) => void): Promise<void> {
    console.log('[GameEngine] Loading assets...');

    onProgress?.(0.5, 'Loading character sprites...');
    const charNames = ['alex', 'sophia', 'bob', 'emily', 'jack', 'lucy', 'mason', 'olivia'];
    await this.spriteLoader.load(charNames, '/agent-valley/character_assets/', (p) => {
      onProgress?.(0.5 + p * 0.5, 'Loading character sprites...');
    });

    this.onLayoutChange?.({ sceneW: this.sceneW, sceneH: this.sceneH });
    onProgress?.(1, 'Ready!');
    console.log('[GameEngine] Assets loaded');
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
      this._createNPC(agent);
    });

    this.onLayoutChange?.({ sceneW: this.sceneW, sceneH: this.sceneH });
  }

  private _createNPC(agent: Agent): void {
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

    const container = new PIXI.Container();
    container.addChild(shadow);
    container.addChild(sprite);
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
    };

    this.npcs.push(npcSprite);
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
        this._updateNPCAnimation(existing);
      } else {
        this._createNPC(agent);
      }
    });

    this._agentsById = agentMap;
  }

  private _updateNPCAnimation(npc: NPCSprite): void {
    const agent = npc.agent;
    const frames = this.spriteLoader.charFrames[npc.charName];
    if (!frames) return;

    let newAnim: 'idle' | 'run' | 'phone' = 'idle';
    let animFrames: PIXI.Texture[];
    let animSpeed = 0.15;

    if (agent.status === 'working') {
      if (frames.phone.length > 0) {
        newAnim = 'phone';
        animFrames = frames.phone;
        animSpeed = 0.12;
      } else {
        newAnim = 'run';
        animFrames = frames.right;
        animSpeed = 0.18;
      }
    } else {
      newAnim = 'idle';
      animFrames = frames.idle;
      animSpeed = 0.1;
    }

    if (npc.currentAnim !== newAnim) {
      npc.currentAnim = newAnim;
      npc.sprite.textures = animFrames;
      npc.sprite.animationSpeed = animSpeed;
      npc.sprite.play();
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

  destroy(): void {
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
  }
}
