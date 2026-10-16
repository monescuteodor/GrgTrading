// Test pe PC pentru evaluator: g++ -I../calculator_gpt test_calc_eval.cpp && ./a.out
#include <cstdio>
#include <cmath>
#include "calc_eval.h"

static int failures = 0;
static void ok(const char* e, double want, double ans = 0) {
  EvalResult r = evaluateExpression(e, ans);
  if (!r.ok || fabs(r.value - want) > 1e-9) { printf("FAIL %s -> %s %g (vrut %g)\n", e, r.ok ? "ok" : r.error, r.value, want); failures++; }
}
static void bad(const char* e) {
  EvalResult r = evaluateExpression(e, 0);
  if (r.ok) { printf("FAIL %s ar fi trebuit sa dea eroare, a dat %g\n", e, r.value); failures++; }
}
int main() {
  ok("1+2*3", 7); ok("(1+2)*3", 9); ok("10/4", 2.5); ok("-2^2", -4); ok("2^-1", 0.5);
  ok("2^3^2", 512); ok("r9", 3); ok("r9^2", 9); ok("2r9", 6); ok("2(3+1)", 8);
  ok("(2+3", 5); ok(".5+1", 1.5); ok("a*2", 10, 5); ok("3-(-2)", 5); ok("r(16)", 4);
  ok("0.1+0.2", 0.3); ok("--3", 3);
  bad(""); bad("2+"); bad("1/0"); bad("1.2.3"); bad("()"); bad("2)"); bad("r-4"); bad("*3");
  bad("10^400");
  printf(failures ? "%d teste picate\n" : "Toate testele au trecut\n", failures);
  return failures != 0;
}
