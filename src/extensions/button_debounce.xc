/*
 * button_press.xc
 *
 *  Created on: 18. mars 2015
 *      Author: teig
 */
#define INCLUDES
#ifdef INCLUDES
    #include <platform.h>
    #include <xs1.h>
    #include <stdlib.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <iso646.h>
    #include <xccompat.h> // REFERENCE_PARAM

    #include "_version.h" // First this..
    #include "_globals.h" // ..then this
    #include "param.h"
    #include "button_debounce.h"
#endif

#define DEBUG_PRINT_BUTTON_PRESS 0
#define debug_print(fmt, ...) do { if((DEBUG_PRINT_BUTTON_PRESS==1) and (DEBUG_PRINT_GLOBAL_APP==1)) printf(fmt, __VA_ARGS__); } while (0)

#define DEBOUNCE_TIMEOUT_MS 50
#define BUTTON_PRESSED   0 // If pullup resistor
#define BUTTON_RELEASED  1 // If pullup resistor

/* COPYRIGHT (C) Øyvind Teig
 * Source: https://www.teigfam.net/oyvind/home/technology/214-my-button-presses-vs-bounce-vs-emi-notes/
 * COPYRIGHT (C) SPECIALTYCIRCUITS LLC 2025
 * Modified for using chanend instead of interface
 */

[[combinable]]
void button_debounce_task (
        const unsigned              button_n,
        const long_button_enabled_e long_button_enabled,
        in buffered port:1          p_button,
        client button_if_2          i_button_out)
{
    int      button_on_event = BUTTON_PRESSED;
    bool     do_timeout_debounce_now = false;
    bool     do_timeout_long_now = false;
    bool     filter_next_button_released = true; // This would come initially
    timer    tmr_debounce; // Only one combined hardware timer..
    timer    tmr_long;     // ..is used for these two software timers
    time32_t timeout_debounce;
    time32_t timeout_long;
    time32_t current_time;
    unsigned button_edge_cnt = 0;

    debug_print("inP_Button_Task_2[%u] started\n", button_n);

    while(1) {
        select {
            case p_button when pinsneq(button_on_event) :> button_on_event: {
                do_timeout_debounce_now = true;
                do_timeout_long_now     = false;
                button_edge_cnt++;
                tmr_debounce :> current_time;
                timeout_debounce = current_time + (DEBOUNCE_TIMEOUT_MS * XS1_TIMER_KHZ);
            } break;

            case do_timeout_debounce_now => tmr_debounce when timerafter(timeout_debounce) :> void: {
                do_timeout_debounce_now = false;
                if (button_on_event == BUTTON_PRESSED) {
                    debug_print(" BUTTON_ACTION_PRESSED %u send, cnt %u\n", button_n, button_edge_cnt);
                    i_button_out.button (BUTTON_ACTION_PRESSED, button_edge_cnt); // Button down
                    if (long_button_enabled == long_enabled) {
                        do_timeout_long_now = true;
                        tmr_long :> current_time;
                        timeout_long = current_time + (BUTTON_ACTION_PRESSED_FOR_LONG_TIMEOUT_MS * XS1_TIMER_KHZ);
                    } else {
                        // long_disabled, no code
                    }
                } else if (filter_next_button_released) {
                    // BUTTON_RELEASED, but we don't want it after BUTTON_ACTION_PRESSED_FOR_LONG, no code
                    debug_print(" BUTTON_ACTION_RELEASED %u filtered\n", button_n);
                } else { // BUTTON_RELEASED
                    debug_print(" BUTTON_ACTION_RELEASED %u send, cnt %u\n", button_n, button_edge_cnt);
                    i_button_out.button (BUTTON_ACTION_RELEASED, button_edge_cnt);
                }
                filter_next_button_released = false;
                button_edge_cnt = 0;
            } break;

            case do_timeout_long_now => tmr_long when timerafter(timeout_long) :> void: {
                do_timeout_long_now = false;
                if (button_on_event == BUTTON_PRESSED) {
                    debug_print(" BUTTON_ACTION_PRESSED_FOR_LONG %u send, cnt %u\n", button_n, button_edge_cnt);
                    i_button_out.button (BUTTON_ACTION_PRESSED_FOR_LONG, button_edge_cnt);
                    filter_next_button_released = true;
                } else { // BUTTON_RELEASED
                    // cannot happen here since do_timeout_long_now was set when BUTTON_PRESSED, no code
                    debug_print(" BUTTON_ACTION_RELEASED_FOR_LONG %u NOT sent, cnt %u\n", button_n, button_edge_cnt);
                }
                button_edge_cnt = 0;
            } break;
        }
    }
}