export class ComciganError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ComciganError';
  }
}

export class ParseError extends ComciganError {
  constructor(message: string) {
    super(message);
    this.name = 'ParseError';
  }
}

export class SchoolNotFoundError extends ComciganError {
  constructor(message: string) {
    super(message);
    this.name = 'SchoolNotFoundError';
  }
}

export class TimetableError extends ComciganError {
  code?: number;
  constructor(message: string, code?: number) {
    super(message);
    this.name = 'TimetableError';
    this.code = code;
  }
}
