import type { CaseRow } from './types';

export function caseNeedsAnalysis(c: CaseRow): boolean {
  return !c.steps?.length || c.sync_status !== 'synced';
}

export function caseIsSelectable(c: CaseRow): boolean {
  return c.status !== 'rejected' && c.status !== 'deprecated';
}

export function caseIsApproved(c: CaseRow): boolean {
  return caseIsSelectable(c)
    && c.status === 'active'
    && !caseNeedsAnalysis(c);
}

export function caseNeedsReview(c: CaseRow): boolean {
  return caseIsSelectable(c) && !caseIsApproved(c);
}

export function caseCanAnalyze(c: CaseRow): boolean {
  return caseIsSelectable(c);
}
