/*
 * The random name a form starts with, when the thing being created needs one.
 *
 * A client and an API token are both named by whoever adds them, and in both
 * cases the name is an identifier: the client's is what its config file is
 * called and what every per-client route is addressed by, the token's is what
 * the activity log writes beside everything it does. An empty box in front of
 * somebody adding the fortieth client of an afternoon is a demand to invent a
 * fortieth word, and what it produces is "test2".
 *
 * So the box opens with a name already in it, and this is where it comes from.
 * The operator saves it as it stands or types over it - both are one action,
 * which is the whole point of putting a value there rather than a placeholder.
 *
 * The same nine characters and the same alphabet as the server's own generator
 * in awg/names.py, deliberately: a name suggested here and a name drawn by the
 * server when the box is left empty must be the same kind of thing, or the two
 * halves of one feature would read as two features. That file explains the
 * alphabet - lower case, and without the four characters that get read wrong
 * when a name is read off a screen and typed back somewhere else.
 *
 * This is not the collision check. Nothing in a browser can make one: the list
 * is paginated, it is seconds old, and another admin may be adding a client in
 * the next room. The server checks, under its config lock, and refuses a name
 * that is taken - which for a name drawn from forty-five bits is a reply nobody
 * will ever see, and is still the reply that makes the name an identifier.
 */

const ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789";

export const NAME_LENGTH = 9;

/**
 * One suggestion.
 *
 * From `crypto.getRandomValues` rather than `Math.random`, for the reason the
 * server uses `secrets`: nothing here is a secret, but a suggestion that
 * repeats - across two tabs opened at the same moment, or a browser that seeds
 * a weak generator predictably - is exactly the collision the name is supposed
 * not to have. The modulo is unbiased because the alphabet's length divides 256.
 */
export function randomName(length: number = NAME_LENGTH): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => ALPHABET[byte % ALPHABET.length]).join("");
}

/**
 * Whether a key event is somebody typing a character into the box.
 *
 * Which is what a suggestion should give way to. Not an arrow key, not a
 * backspace - those are somebody editing what is there, and the length of
 * `key` is what tells the two apart - and not a shortcut, where the character
 * belongs to the browser rather than to the field.
 *
 * Structurally typed rather than taking React's event, so that this file goes
 * on knowing nothing about the framework.
 */
export function typedCharacter(event: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
}): boolean {
  return event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;
}

/**
 * Have the character about to arrive replace the whole suggestion, rather than
 * land next to it.
 *
 * Called from the box's own keydown and paste, a moment before the browser
 * inserts anything - selecting the text there and then is what makes the
 * insertion replace it, and nothing is on screen long enough to be seen.
 *
 * The obvious way to get the same effect is to select the box when the dialog
 * opens, and it is worse: nine characters nobody typed, sitting highlighted in
 * a form that has only just appeared, read as something the panel is warning
 * about rather than as a value offered. So the box opens quietly and the
 * replacement waits for the keystroke that needs it.
 *
 * Only while the box still holds the suggestion exactly as it was drawn, and
 * only with the caret collapsed: a value somebody has edited is theirs, and a
 * selection they made by hand is a decision about which part of it to change.
 */
export function replaceSuggestion(box: HTMLInputElement, suggestion: string): void {
  if (suggestion && box.value === suggestion && box.selectionStart === box.selectionEnd) {
    box.select();
  }
}
