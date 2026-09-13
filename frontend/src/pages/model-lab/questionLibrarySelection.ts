export const QUESTION_LIBRARY_SELECTION_KEY = "pronoia.qa-library-selection.v1";

export interface QuestionLibrarySelection {
  questionSetId: string;
  questionIds: string[];
}

// Treat browser storage as an untrusted draft, never as an instruction to run.
export function parseQuestionLibrarySelection(raw: string | null): QuestionLibrarySelection | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object") return null;
    const draft = value as Partial<QuestionLibrarySelection>;
    if (typeof draft.questionSetId !== "string" || !draft.questionSetId.trim()) return null;
    if (!Array.isArray(draft.questionIds) || !draft.questionIds.length || draft.questionIds.length > 2000) return null;
    if (!draft.questionIds.every((id) => typeof id === "string" && id.trim())) return null;
    return { questionSetId: draft.questionSetId, questionIds: [...new Set(draft.questionIds)] };
  } catch {
    return null;
  }
}

export function readQuestionLibrarySelection(): QuestionLibrarySelection | null {
  try {
    return parseQuestionLibrarySelection(window.sessionStorage.getItem(QUESTION_LIBRARY_SELECTION_KEY));
  } catch {
    return null;
  }
}
