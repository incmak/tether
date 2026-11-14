// The periodic lessons review (server: lessons.review, /mobile/api/autonomy/review). Tether asks the person to review
// all lessons every 14 days, after 5 new lessons, or when an agent disputes one. It never opens anything by itself.
import { useCallback } from 'react'
import type { Api } from './api'
import { usePolling } from './hooks'
import type { Lesson, ReviewSummary } from './Autonomy'

export type ReviewGroupKey = 'disputed' | 'stuck' | 'stale' | 'new' | 'other'
export interface ReviewLesson extends Lesson {
  project: string | null; project_name: string | null
  flags?: { type: 'conflict' | 'repeat'; with: string }[]
}
export interface ReviewState extends ReviewSummary {
  last_review: number; snoozed_until: number
  groups: { key: ReviewGroupKey; lessons: ReviewLesson[] }[]
  skipped?: string[]
  check?: LessonsCheck | null
}

// The advisor check (server: lessons_check.py). The advisor only suggests; the person still decides each lesson.
export type CheckVerdict = 'keep' | 'edit' | 'remove' | 'unsure'
export interface CheckAdvice { verdict: CheckVerdict; confidence: 'high' | 'medium' | 'low'; reason: string; text?: string }
export interface LessonsCheck {
  state: 'idle' | 'running' | 'done' | 'failed'
  started?: number; finished?: number | null; label?: string; count?: number; skipped?: number; error?: string
  advice?: Record<string, CheckAdvice>
}

/** How many lessons the review card names: the new and disputed ones, or all of them for the 14-day review. */
export const reviewCount = (review: ReviewSummary) => review.reason === 'time' ? review.total : review.new + review.disputed

export function reviewReason(review: ReviewSummary) {
  const count = reviewCount(review)
  if (review.reason === 'disputed') return review.disputed === 1 ? 'An agent questioned a lesson' : `Agents questioned ${review.disputed} lessons`
  if (review.reason === 'new') return `${count} new lessons since your last review`
  if (review.reason === 'time') return 'Two weeks since your last review'
  return ''
}

/** The review state, polled once a minute. Off: no requests. */
export function useLessonsReview(api: Api, enabled = true) {
  const fetcher = useCallback((signal: AbortSignal) => enabled ? api<ReviewState>('autonomy/review', undefined, signal) : Promise.resolve(null), [api, enabled])
  return usePolling<ReviewState | null>(fetcher, 60000)
}
