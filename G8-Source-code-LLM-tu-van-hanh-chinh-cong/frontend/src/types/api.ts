export type User = { id: string; display_name: string; username?: string | null; email?: string | null; is_guest?: boolean }
export type Conversation = { id: string; title: string; topic?: string | null; is_pinned?: boolean; created_at: string; updated_at: string }
export type AuthConfig = { password_enabled: boolean; guest_enabled: boolean }
export type Registration = { username: string; display_name: string; password: string; password_confirmation: string }
export type GroundingStatus = 'ANSWER' | 'NEED_CLARIFICATION' | 'INSUFFICIENT_DATA'
export type Citation = {
  fragment_id: string
  source_id: string
  title: string
  url: string | null
  metadata: Record<string, unknown>
}
export type Grounding = {
  parts?: { task_id: string; result_status?: string; kind: string; procedure_id: string | null; title: string; answer: string; grounding: Grounding }[] | null
  request_id: string
  evidence_bundle_id: string
  status: GroundingStatus
  corpus_version: string
  data_classification: 'D0_SYSTEM_SMOKE' | 'D1_PUBLIC_DEMO' | 'D2_COMPANY_REAL'
  prompt_version?: string | null
  provider?: string | null
  model?: string | null
  sources: Citation[]
  procedure_title?: string | null
  missing_information: string[]
  verification?: {
    plan_version: 'g4-answer-plan-v1'
    policy: 'full-claim-exact-v1'
    candidate_checked: boolean
    candidate_passed: boolean
    fallback_used: boolean
    reasons: string[]
  } | null
  checklist?: {
    field: string
    label: string
    value: string
    evidence_ids: string[]
  }[] | null
  knowledge_version?: string | null
  source_checked_at?: string | null
}
export type Message = {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  status: 'pending' | 'completed' | 'failed'
  created_at: string
  grounding?: Grounding | null
}
export type CreateMessageInput = { client_message_id: string; content: string }
export type CreateMessageResponse = { user_message: Message; assistant_message: Message }
export type FeedbackRelevance = 'relevant' | 'not_relevant'
export type FeedbackSatisfaction = 'satisfied' | 'not_satisfied'
export type MessageFeedback = {
  id: string
  message_id: string
  relevance: FeedbackRelevance
  satisfaction: FeedbackSatisfaction
  reason?: string | null
  provider?: string | null
  model?: string | null
  corpus_version?: string | null
  prompt_version?: string | null
  created_at: string
  updated_at: string
}
export type FeedbackInput = {
  relevance: FeedbackRelevance
  satisfaction: FeedbackSatisfaction
  reason?: string | null
}

export type PreparationStatus = 'unknown' | 'have' | 'missing' | 'not_applicable'
export type ReviewedForm = {
  id: string; title: string; form_number: string; legal_basis: string; source_url: string
  issuing_authority: string; jurisdiction: string; applicability: string; extraction_note: string
  checked_at: string; review_due_at: string; bytes: number
}
export type PreparationChecklist = {
  id: string; title: string; procedure_id: string; conversation_id: string | null
  corpus_version: string; source_changed: boolean; revision: number; guidance: string
  created_at: string; updated_at: string; counts: Record<PreparationStatus, number>
  items: { id: string; text: string; section: string; evidence_ids: string[]; status: PreparationStatus; status_source: string; forms?: ReviewedForm[] }[]
}
