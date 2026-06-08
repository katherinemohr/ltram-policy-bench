CXX      = g++
CXXFLAGS = -O2 -std=c++17 -I.
LDFLAGS  = -L. -Wl,-rpath,'$$ORIGIN'
LIBS     = -lduckdb -lstdc++ -lm -ldl -lpthread

TARGET = bench

all: $(TARGET)

$(TARGET): bench.cpp duckdb.h
	$(CXX) $(CXXFLAGS) bench.cpp -o $(TARGET) $(LDFLAGS) $(LIBS)

clean:
	rm -f $(TARGET)
