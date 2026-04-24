export class PositionAllocator {
  private occupiedPositions: Array<{ x: number; y: number }> = [];
  private readonly minDistance = 150; // 最小间距

  allocate(sceneW: number, sceneH: number): { x: number; y: number } {
    // 如果是第一个位置，使用主 agent 的位置附近
    if (this.occupiedPositions.length === 0) {
      const pos = { x: 500, y: 400 };
      this.occupiedPositions.push(pos);
      return pos;
    }

    // 尝试 20 次找到最佳位置
    let bestPos = null;
    let maxMinDist = 0;

    for (let i = 0; i < 20; i++) {
      const candidate = {
        x: 250 + Math.random() * (sceneW - 500),
        y: 350 + Math.random() * (sceneH - 500),
      };

      // 计算到所有已占用位置的最小距离
      let minDist = Infinity;
      for (const pos of this.occupiedPositions) {
        const dist = Math.sqrt(
          Math.pow(candidate.x - pos.x, 2) +
          Math.pow(candidate.y - pos.y, 2)
        );
        minDist = Math.min(minDist, dist);
      }

      if (minDist > maxMinDist) {
        maxMinDist = minDist;
        bestPos = candidate;
      }
    }

    if (bestPos) {
      this.occupiedPositions.push(bestPos);
      return bestPos;
    }

    // 降级方案：随机位置
    const fallback = {
      x: 250 + Math.random() * (sceneW - 500),
      y: 350 + Math.random() * (sceneH - 500),
    };
    this.occupiedPositions.push(fallback);
    return fallback;
  }

  release(position: { x: number; y: number }): void {
    const index = this.occupiedPositions.findIndex(
      p => Math.abs(p.x - position.x) < 1 && Math.abs(p.y - position.y) < 1
    );
    if (index >= 0) {
      this.occupiedPositions.splice(index, 1);
    }
  }

  clear(): void {
    this.occupiedPositions = [];
  }
}
