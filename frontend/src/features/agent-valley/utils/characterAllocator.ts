const AVAILABLE_CHARACTERS = ['Alex', 'Bob', 'Lucy', 'Adam', 'Amelia'];

export class CharacterAllocator {
  private characterIndex = 0;

  allocate(): string {
    // 轮询分配，确保每个子任务使用不同的角色
    const char = AVAILABLE_CHARACTERS[this.characterIndex % AVAILABLE_CHARACTERS.length];
    this.characterIndex++;
    return char;
  }

  reset(): void {
    this.characterIndex = 0;
  }
}
