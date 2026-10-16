// Evaluator de expresii pentru calculator (fara dependinte Arduino, testabil pe PC).
//
// Simboluri interne:
//   cifre  .  + - * / ^ ( )
//   'r' = radical (sqrt), 'a' = Ans (rezultatul anterior)
// Inmultirea implicita e permisa: 2(3+1), 3r9, 2a
#pragma once

#include <ctype.h>
#include <math.h>
#include <stdlib.h>

struct EvalResult {
  bool ok;
  double value;
  const char* error;
};

class ExprParser {
 public:
  ExprParser(const char* s, double ans) : s_(s), pos_(0), ans_(ans), err_(nullptr) {}

  EvalResult run() {
    double v = expr();
    if (!err_ && s_[pos_] != '\0') fail("Sintaxa gresita");
    if (!err_ && (isnan(v) || isinf(v))) fail("Eroare matematica");
    return {err_ == nullptr, err_ ? 0.0 : v, err_};
  }

 private:
  const char* s_;
  int pos_;
  double ans_;
  const char* err_;

  char peek() const { return s_[pos_]; }

  double fail(const char* msg) {
    if (!err_) err_ = msg;
    return 0;
  }

  // expr := term (('+' | '-') term)*
  double expr() {
    double v = term();
    while (!err_) {
      char c = peek();
      if (c == '+') { pos_++; v += term(); }
      else if (c == '-') { pos_++; v -= term(); }
      else break;
    }
    return v;
  }

  // term := unary (('*' | '/' | <implicit>) unary)*
  double term() {
    double v = unary();
    while (!err_) {
      char c = peek();
      if (c == '*') { pos_++; v *= unary(); }
      else if (c == '/') {
        pos_++;
        double d = unary();
        if (err_) break;
        if (d == 0) return fail("Impartire la 0");
        v /= d;
      }
      else if (c == '(' || c == 'r' || c == 'a' || isdigit((unsigned char)c)) { v *= unary(); }
      else break;
    }
    return v;
  }

  // unary := ('-' | '+') unary | power
  double unary() {
    if (peek() == '-') { pos_++; return -unary(); }
    if (peek() == '+') { pos_++; return unary(); }
    return power();
  }

  // power := primary ('^' unary)?    (asociativ la dreapta: 2^3^2 = 2^9)
  double power() {
    double b = primary();
    if (!err_ && peek() == '^') {
      pos_++;
      double e = unary();
      return pow(b, e);
    }
    return b;
  }

  // primary := number | 'a' | 'r' primary | '(' expr ')'?
  double primary() {
    char c = peek();
    if (c == '(') {
      pos_++;
      double v = expr();
      if (peek() == ')') pos_++;  // paranteza de inchidere e optionala la final
      return v;
    }
    if (c == 'r') {
      pos_++;
      double v = (peek() == '-') ? unary() : primary();
      if (err_) return 0;
      if (v < 0) return fail("Radical din negativ");
      return sqrt(v);
    }
    if (c == 'a') {
      pos_++;
      return ans_;
    }
    if (isdigit((unsigned char)c) || c == '.') {
      char* end = nullptr;
      double v = strtod(s_ + pos_, &end);
      if (end == s_ + pos_) return fail("Sintaxa gresita");
      pos_ = (int)(end - s_);
      return v;
    }
    return fail("Sintaxa gresita");
  }
};

inline EvalResult evaluateExpression(const char* s, double ans) {
  ExprParser p(s, ans);
  return p.run();
}
