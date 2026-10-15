#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    int sum = 0;
    for (int i = 1; i < argc; i++) sum += atoi(argv[i]);
    printf("%.6f\n", argc > 1 ? (double)(sum / (argc - 1)) : 0.0);
    return 0;
}
