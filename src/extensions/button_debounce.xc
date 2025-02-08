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
    #include <stdint.h>
    #include <stdio.h>
    #include <iso646.h>
    #include <xccompat.h> // REFERENCE_PARAM
    #include <debug_print.h>    

    //#include "_version.h" // First this..
    //#include "_globals.h" // ..then this
    //#include "param.h"
    #include "button_debounce.h"
#endif

#define DEBOUNCE_TIMEOUT_MS 50
#define BUTTON_PRESSED   0 // If pullup resistor
#define BUTTON_RELEASED  1 // If pullup resistor

/* COPYRIGHT (C) �yvind Teig
 * Source: https://www.teigfam.net/oyvind/home/technology/214-my-button-presses-vs-bounce-vs-emi-notes/
 * COPYRIGHT (C) SPECIALTYCIRCUITS LLC 2025
 * Modified for using chanend instead of interface
 */

[[combinable]]
void button_debounce_task ( const unsigned              button_n,
                            const long_button_enabled_e long_button_enabled,
                            in buffered port:1          p_button,
                            chanend                     i_button_out)
{
    int      button_on_event = BUTTON_PRESSED;
    int      do_timeout_debounce_now = 0;
    int      do_timeout_long_now = 0;
    int      filter_next_button_released = 1 ; // This would come initially
    timer    tmr_debounce; // Only one combined hardware timer..
    timer    tmr_long;     // ..is used for these two software timers
    int32_t  timeout_debounce;
    int32_t  timeout_long;
    int32_t  current_time;
    unsigned button_edge_cnt = 0;

    debug_printf("inP_Button_Task_2[%u] started\n", button_n);

    while(1) {
        select {
            case p_button when pinsneq(button_on_event) :> button_on_event: {
                do_timeout_debounce_now = 1 ;
                do_timeout_long_now     = 0;
                button_edge_cnt++;
                tmr_debounce :> current_time;
                timeout_debounce = current_time + (DEBOUNCE_TIMEOUT_MS * XS1_TIMER_KHZ);
            } break;

            case do_timeout_debounce_now => tmr_debounce when timerafter(timeout_debounce) :> void: {
                do_timeout_debounce_now = 0;
                if (button_on_event == BUTTON_PRESSED) {
                    debug_printf(" BUTTON_ACTION_PRESSED %u send, cnt %u\n", button_n, button_edge_cnt);
                    // i_button_out.button (BUTTON_ACTION_PRESSED, button_edge_cnt); // Button down
                    {
                        i_button_out <: BUTTON_ACTION_PRESSED;
                        i_button_out <: button_edge_cnt;
                    }
                    if (long_button_enabled == long_enabled) {
                        do_timeout_long_now = 1 ;
                        tmr_long :> current_time;
                        timeout_long = current_time + (BUTTON_ACTION_PRESSED_FOR_LONG_TIMEOUT_MS * XS1_TIMER_KHZ);
                    } else {
                        // long_disabled, no code
                    }
                } else if (filter_next_button_released) {
                    // BUTTON_RELEASED, but we don't want it after BUTTON_ACTION_PRESSED_FOR_LONG, no code
                    debug_printf(" BUTTON_ACTION_RELEASED %u filtered\n", button_n);
                } else { // BUTTON_RELEASED
                    debug_printf(" BUTTON_ACTION_RELEASED %u send, cnt %u\n", button_n, button_edge_cnt);
                    // i_button_out.button (BUTTON_ACTION_RELEASED, button_edge_cnt);
                    {
                        i_button_out <: BUTTON_ACTION_RELEASED;
                        i_button_out <: button_edge_cnt;
                    }
                }
                filter_next_button_released = 0;
                button_edge_cnt = 0;
            } break;

            case do_timeout_long_now => tmr_long when timerafter(timeout_long) :> void: {
                do_timeout_long_now = 0;
                if (button_on_event == BUTTON_PRESSED) {
                    debug_printf(" BUTTON_ACTION_PRESSED_FOR_LONG %u send, cnt %u\n", button_n, button_edge_cnt);
                    {
                        i_button_out <: BUTTON_ACTION_PRESSED_FOR_LONG;
                        i_button_out <: button_edge_cnt;
                    }
                    filter_next_button_released = 1 ;
                } else { // BUTTON_RELEASED
                    // cannot happen here since do_timeout_long_now was set when BUTTON_PRESSED, no code
                    debug_printf(" BUTTON_ACTION_RELEASED_FOR_LONG %u NOT sent, cnt %u\n", button_n, button_edge_cnt);
                }
                button_edge_cnt = 0;
            } break;
        }
    }
}

// [[combinable]]
// void button_debounce_task ( const unsigned              button_n,
//                             const long_button_enabled_e long_button_enabled,
//                             in buffered port:1          p_button,
//                             client button_if_2          i_button_out)
// {
//     int      button_on_event = BUTTON_PRESSED;
//     int      do_timeout_debounce_now = 0;
//     int      do_timeout_long_now = 0;
//     int      filter_next_button_released = 1 ; // This would come initially
//     timer    tmr_debounce; // Only one combined hardware timer..
//     timer    tmr_long;     // ..is used for these two software timers
//     int32_t  timeout_debounce;
//     int32_t  timeout_long;
//     int32_t  current_time;
//     unsigned button_edge_cnt = 0;

//     debug_printf("inP_Button_Task_2[%u] started\n", button_n);

//     while(1) {
//         select {
//             case p_button when pinsneq(button_on_event) :> button_on_event: {
//                 do_timeout_debounce_now = 1 ;
//                 do_timeout_long_now     = 0;
//                 button_edge_cnt++;
//                 tmr_debounce :> current_time;
//                 timeout_debounce = current_time + (DEBOUNCE_TIMEOUT_MS * XS1_TIMER_KHZ);
//             } break;

//             case do_timeout_debounce_now => tmr_debounce when timerafter(timeout_debounce) :> void: {
//                 do_timeout_debounce_now = 0;
//                 if (button_on_event == BUTTON_PRESSED) {
//                     debug_printf(" BUTTON_ACTION_PRESSED %u send, cnt %u\n", button_n, button_edge_cnt);
//                     i_button_out.button (BUTTON_ACTION_PRESSED, button_edge_cnt); // Button down
//                     if (long_button_enabled == long_enabled) {
//                         do_timeout_long_now = 1 ;
//                         tmr_long :> current_time;
//                         timeout_long = current_time + (BUTTON_ACTION_PRESSED_FOR_LONG_TIMEOUT_MS * XS1_TIMER_KHZ);
//                     } else {
//                         // long_disabled, no code
//                     }
//                 } else if (filter_next_button_released) {
//                     // BUTTON_RELEASED, but we don't want it after BUTTON_ACTION_PRESSED_FOR_LONG, no code
//                     debug_printf(" BUTTON_ACTION_RELEASED %u filtered\n", button_n);
//                 } else { // BUTTON_RELEASED
//                     debug_printf(" BUTTON_ACTION_RELEASED %u send, cnt %u\n", button_n, button_edge_cnt);
//                     i_button_out.button (BUTTON_ACTION_RELEASED, button_edge_cnt);
//                 }
//                 filter_next_button_released = 0;
//                 button_edge_cnt = 0;
//             } break;

//             case do_timeout_long_now => tmr_long when timerafter(timeout_long) :> void: {
//                 do_timeout_long_now = 0;
//                 if (button_on_event == BUTTON_PRESSED) {
//                     debug_printf(" BUTTON_ACTION_PRESSED_FOR_LONG %u send, cnt %u\n", button_n, button_edge_cnt);
//                     i_button_out.button (BUTTON_ACTION_PRESSED_FOR_LONG, button_edge_cnt);
//                     filter_next_button_released = 1 ;
//                 } else { // BUTTON_RELEASED
//                     // cannot happen here since do_timeout_long_now was set when BUTTON_PRESSED, no code
//                     debug_printf(" BUTTON_ACTION_RELEASED_FOR_LONG %u NOT sent, cnt %u\n", button_n, button_edge_cnt);
//                 }
//                 button_edge_cnt = 0;
//             } break;
//         }
//     }
// }