/* Render and read back a pixel with NVIDIA's EGL device platform.
 *
 * Build: cc -std=c11 -Wall -Wextra -Werror -O2 tests/check_egl.c \
 *          -o /tmp/check-nvidia-egl $(pkg-config --cflags --libs egl glesv2)
 * Run as the desktop user. The test uses NVIDIA's EGL vendor library and
 * a pbuffer, without connecting to Wayland or X11.
 */
#define _POSIX_C_SOURCE 200809L
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static GLuint compile_shader(GLenum type, const char *source)
{
    GLuint shader = glCreateShader(type);
    GLint compiled = GL_FALSE;
    if (!shader) {
        fprintf(stderr, "Shader creation failed: 0x%x\n", glGetError());
        return 0;
    }
    glShaderSource(shader, 1, &source, NULL);
    glCompileShader(shader);
    glGetShaderiv(shader, GL_COMPILE_STATUS, &compiled);
    if (!compiled) {
        char log[4096];
        glGetShaderInfoLog(shader, sizeof(log), NULL, log);
        fprintf(stderr, "Shader compilation failed: %s\n", log);
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

int main(void)
{
    EGLDisplay display = EGL_NO_DISPLAY;
    EGLSurface surface = EGL_NO_SURFACE;
    EGLContext context = EGL_NO_CONTEXT;
    EGLDeviceEXT devices[16];
    EGLint count = 0, major = 0, minor = 0, configurations = 0;
    EGLConfig config;
    GLuint vertex = 0, fragment = 0, program = 0, buffer = 0;
    int current = 0, result = 1;
    const EGLint config_attributes[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE
    };
    const EGLint pbuffer_attributes[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
    const EGLint context_attributes[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    const GLfloat triangle[] = {-1.0f, -1.0f, 3.0f, -1.0f, -1.0f, 3.0f};
    const unsigned char expected[] = {64, 128, 191, 255};
    unsigned char pixel[4] = {0};

    if (setenv("__EGL_VENDOR_LIBRARY_FILENAMES",
               "/usr/share/glvnd/egl_vendor.d/10_nvidia.json", 1) ||
        unsetenv("DISPLAY") || unsetenv("WAYLAND_DISPLAY")) {
        perror("Setting the EGL environment");
        return 1;
    }

    PFNEGLQUERYDEVICESEXTPROC query_devices =
        (PFNEGLQUERYDEVICESEXTPROC)eglGetProcAddress("eglQueryDevicesEXT");
    PFNEGLGETPLATFORMDISPLAYEXTPROC platform_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (!query_devices || !platform_display ||
        !query_devices(16, devices, &count) || count < 1) {
        fprintf(stderr, "NVIDIA EGL device enumeration failed: 0x%x\n", eglGetError());
        return 1;
    }
    display = platform_display(EGL_PLATFORM_DEVICE_EXT, devices[0], NULL);
    if (display == EGL_NO_DISPLAY || !eglInitialize(display, &major, &minor) ||
        !eglBindAPI(EGL_OPENGL_ES_API) ||
        !eglChooseConfig(display, config_attributes, &config, 1, &configurations) ||
        configurations != 1) {
        fprintf(stderr, "EGL initialization failed: 0x%x\n", eglGetError());
        goto cleanup;
    }
    surface = eglCreatePbufferSurface(display, config, pbuffer_attributes);
    context = eglCreateContext(display, config, EGL_NO_CONTEXT, context_attributes);
    if (surface == EGL_NO_SURFACE || context == EGL_NO_CONTEXT ||
        !eglMakeCurrent(display, surface, surface, context)) {
        fprintf(stderr, "EGL context creation failed: 0x%x\n", eglGetError());
        goto cleanup;
    }
    current = 1;
    const char *vendor = (const char *)glGetString(GL_VENDOR);
    const char *renderer = (const char *)glGetString(GL_RENDERER);
    const char *version = (const char *)glGetString(GL_VERSION);
    if (!vendor || !renderer || !version || !strstr(vendor, "NVIDIA")) {
        fprintf(stderr, "The context is not a working NVIDIA context\n");
        goto cleanup;
    }
    printf("EGL %d.%d, %s\nRenderer: %s\nGL: %s\n", major, minor,
           eglQueryString(display, EGL_VENDOR), renderer, version);

    vertex = compile_shader(GL_VERTEX_SHADER,
        "attribute vec2 position;\n"
        "void main() { gl_Position = vec4(position, 0.0, 1.0); }\n");
    fragment = compile_shader(GL_FRAGMENT_SHADER,
        "precision mediump float;\n"
        "void main() { gl_FragColor = vec4(0.25, 0.5, 0.75, 1.0); }\n");
    if (!vertex || !fragment)
        goto cleanup;
    program = glCreateProgram();
    if (!program) {
        fprintf(stderr, "Program creation failed: 0x%x\n", glGetError());
        goto cleanup;
    }
    glAttachShader(program, vertex);
    glAttachShader(program, fragment);
    glBindAttribLocation(program, 0, "position");
    glLinkProgram(program);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (!linked) {
        char log[4096];
        glGetProgramInfoLog(program, sizeof(log), NULL, log);
        fprintf(stderr, "Program linking failed: %s\n", log);
        goto cleanup;
    }
    glUseProgram(program);
    glGenBuffers(1, &buffer);
    if (!buffer) {
        fprintf(stderr, "Buffer creation failed: 0x%x\n", glGetError());
        goto cleanup;
    }
    glBindBuffer(GL_ARRAY_BUFFER, buffer);
    glBufferData(GL_ARRAY_BUFFER, sizeof(triangle), triangle, GL_STATIC_DRAW);
    GLenum setup_error = glGetError();
    if (setup_error != GL_NO_ERROR) {
        fprintf(stderr, "Buffer setup failed: 0x%x\n", setup_error);
        goto cleanup;
    }
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);
    glEnableVertexAttribArray(0);
    glViewport(0, 0, 16, 16);
    glDisable(GL_DITHER);
    glClearColor(1.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glReadPixels(8, 8, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel);
    glFinish();
    GLenum error = glGetError();
    if (error != GL_NO_ERROR) {
        fprintf(stderr, "Rendering failed with GL error 0x%x\n", error);
        goto cleanup;
    }
    printf("Rendered pixel: %u %u %u %u\n", pixel[0], pixel[1], pixel[2], pixel[3]);
    /* Allow one unit for floating-point to RGBA8 rounding. */
    for (int i = 0; i < 4; i++) {
        if (abs((int)pixel[i] - expected[i]) > 1) {
            fprintf(stderr, "The rendered pixel differs from the shader's color\n");
            goto cleanup;
        }
    }
    puts("Headless NVIDIA shader rendering and readback passed.");
    result = 0;

cleanup:
    if (current) {
        if (buffer) glDeleteBuffers(1, &buffer);
        if (program) glDeleteProgram(program);
        if (vertex) glDeleteShader(vertex);
        if (fragment) glDeleteShader(fragment);
        eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    }
    if (context != EGL_NO_CONTEXT) eglDestroyContext(display, context);
    if (surface != EGL_NO_SURFACE) eglDestroySurface(display, surface);
    if (display != EGL_NO_DISPLAY) eglTerminate(display);
    return result;
}
