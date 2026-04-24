import React from 'react';
import type { Subtask } from '../types/subtask';
import './SubtaskDialog.css';

interface SubtaskDialogProps {
  subtask: Subtask;
  onClose: () => void;
}

export function SubtaskDialog({ subtask, onClose }: SubtaskDialogProps) {
  return (
    <div className="card-overlay" onClick={onClose}>
      <div className="subtask-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="subtask-dialog-header">
          <div className="subtask-dialog-title">
            <span className="subtask-dialog-char">{subtask.charName}</span>
            <h3>{subtask.description}</h3>
          </div>
          <button className="subtask-dialog-close" onClick={onClose}>
            CLOSE
          </button>
        </div>

        <div className="subtask-dialog-content">
          <div className="subtask-status-row">
            <span className="subtask-status-label">Status:</span>
            <span className={`subtask-status-badge subtask-status-${subtask.status}`}>
              {subtask.status.toUpperCase()}
            </span>
          </div>

          {subtask.result && (
            <div className="subtask-section">
              <h4>Result:</h4>
              <pre className="subtask-code">{subtask.result}</pre>
            </div>
          )}

          {subtask.error && (
            <div className="subtask-section subtask-error-section">
              <h4>Error:</h4>
              <pre className="subtask-code subtask-error-code">{subtask.error}</pre>
            </div>
          )}

          {!subtask.result && !subtask.error && subtask.status === 'working' && (
            <div className="subtask-section">
              <p className="subtask-working-text">Task is currently running...</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
